"""Job-finished notifications (B26): settings, e-mail, webhook, retries."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, update

from app import notify
from app.config import settings
from app.models import Job
from app.notify import JobNotifier
from app.scheduler import db_now
from dbhelp import with_session
from harness.client import FakeAgent
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}


class Fakes:
    def __init__(self) -> None:
        self.emails: list[tuple[str, str, str]] = []
        self.hooks: list[tuple[str, dict]] = []
        self.fail_hooks = 0

    async def email(self, to: str, subject: str, body: str) -> None:
        self.emails.append((to, subject, body))

    async def hook(self, url: str, payload: dict) -> None:
        if self.fail_hooks:
            self.fail_hooks -= 1
            raise httpx.ConnectError("down")
        self.hooks.append((url, payload))

    def notifier(self) -> JobNotifier:
        return JobNotifier(self.email, self.hook)

    def for_user(self, email: str) -> list[tuple[str, str, str]]:
        return [m for m in self.emails if m[0] == email]

    def hooks_to(self, url: str) -> list[dict]:
        return [p for u, p in self.hooks if u == url]


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    return Fakes()


def _run(fakes: Fakes) -> int:
    async def once(session):
        return await fakes.notifier().run_once(session, await db_now(session))

    return asyncio.run(with_session(once))


def _row(job_id: str) -> Job:
    async def get(session):
        return await session.scalar(select(Job).where(Job.id == uuid.UUID(job_id)))

    return asyncio.run(with_session(get))


def _finish(client, portal: Portal, *, ok: bool = True, lines: list[str] | None = None) -> dict:
    tag = f"nt-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(
        client,
        hostname=f"nt-{tag}",
        caps={"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0},
    )
    agent.register()
    agent.heartbeat()
    job = portal.submit_job(gpu_class=tag)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    if lines:
        agent.post_logs(attempt, lines)
    agent.event(attempt, "uploading")
    if ok:
        agent.complete(attempt)
    else:
        agent.complete(attempt, exit_code=1, failure_category="user_code")
    return job


def _settings(client, portal: Portal, **body):
    return client.put("/me/notifications", json=body, headers=portal.headers)


# --- Settings ----------------------------------------------------------------------


def test_notification_settings_default_to_email_on_and_no_webhook(client, monkeypatch):
    portal = Portal(client)
    monkeypatch.setattr(settings, "smtp_host", "")
    got = client.get("/me/notifications", headers=portal.headers).json()
    assert got == {"notify_email": True, "notify_webhook_url": None, "email_available": False}
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    assert client.get("/me/notifications", headers=portal.headers).json()["email_available"] is True
    assert client.get("/me/notifications").status_code == 401


def test_settings_can_be_changed_partly_and_cleared(client):
    portal = Portal(client)
    resp = _settings(client, portal, notify_webhook_url="  https://hooks.example/abc ")
    assert resp.status_code == 200, resp.text
    assert resp.json()["notify_webhook_url"] == "https://hooks.example/abc" and resp.json()["notify_email"] is True

    off = _settings(client, portal, notify_email=False).json()
    assert off["notify_email"] is False and off["notify_webhook_url"] == "https://hooks.example/abc"  # untouched

    cleared = _settings(client, portal, notify_webhook_url="").json()
    assert cleared["notify_webhook_url"] is None and cleared["notify_email"] is False
    assert client.get("/me/notifications", headers=portal.headers).json()["notify_webhook_url"] is None
    again = _settings(client, portal, notify_webhook_url=None).json()
    assert again["notify_webhook_url"] is None


@pytest.mark.parametrize("url", ["ftp://x.example/h", "javascript:alert(1)", "not a url", "http://", "https://a b.example"])
def test_a_webhook_must_be_an_http_url(client, url):
    assert _settings(client, Portal(client), notify_webhook_url=url).status_code == 422


def test_an_overlong_webhook_is_refused(client):
    assert _settings(client, Portal(client), notify_webhook_url="https://x.example/" + "a" * 600).status_code == 422


# --- Sending -----------------------------------------------------------------------


def test_a_finished_job_is_announced_once_by_email_and_webhook(client, fakes):
    portal = Portal(client)
    _settings(client, portal, notify_webhook_url="https://hooks.example/done")
    job = _finish(client, portal)
    assert _row(job["id"]).notified_at is None

    _run(fakes)
    [(to, subject, body)] = fakes.for_user(portal.email)
    assert to == portal.email and "completed" in subject and job["entrypoint"] in subject
    assert f"/jobs/{job['id']}" in body
    [payload] = fakes.hooks_to("https://hooks.example/done")
    assert payload["event"] == "job.finished" and payload["job_id"] == job["id"]
    assert payload["status"] == "completed" and payload["failure"] is None
    assert payload["url"].endswith(f"/jobs/{job['id']}") and "completed" in payload["text"]
    assert _row(job["id"]).notified_at is not None

    _run(fakes)  # nothing new: no second message
    assert len(fakes.for_user(portal.email)) == 1 and len(fakes.hooks_to("https://hooks.example/done")) == 1


def test_a_failed_job_message_carries_the_explanation(client, fakes):
    portal = Portal(client)
    _settings(client, portal, notify_webhook_url="https://hooks.example/fail")
    job = _finish(client, portal, ok=False, lines=["CUDA out of memory. Tried to allocate 2 GiB"])
    _run(fakes)
    [(_, subject, body)] = fakes.for_user(portal.email)
    assert "failed" in subject and "GPU out of memory" in body
    [payload] = fakes.hooks_to("https://hooks.example/fail")
    assert payload["status"] == "failed" and payload["failure"]["title"] == "GPU out of memory"
    assert payload["job_id"] == job["id"]


def test_email_off_sends_only_the_webhook_and_no_channel_marks_the_job_done(client, fakes):
    portal = Portal(client)
    _settings(client, portal, notify_email=False, notify_webhook_url="https://hooks.example/only")
    _finish(client, portal)
    quiet = Portal(client)
    _settings(client, quiet, notify_email=False)
    quiet_job = _finish(client, quiet)
    _run(fakes)
    assert fakes.for_user(portal.email) == [] and len(fakes.hooks_to("https://hooks.example/only")) == 1
    assert fakes.for_user(quiet.email) == []
    assert _row(quiet_job["id"]).notified_at is not None  # nobody to tell: not looked at again


def test_without_smtp_only_webhooks_go_out(client, fakes, monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "")
    portal = Portal(client)
    _settings(client, portal, notify_webhook_url="https://hooks.example/nosmtp")
    _finish(client, portal)
    _run(fakes)
    assert fakes.for_user(portal.email) == []
    assert len(fakes.hooks_to("https://hooks.example/nosmtp")) == 1


def test_running_queued_and_old_jobs_are_not_announced(client, fakes):
    portal = Portal(client)
    queued = portal.submit_job(gpu_class=f"nt-{uuid.uuid4().hex[:8]}")
    old = _finish(client, portal)

    async def age(session):
        await session.execute(
            update(Job).where(Job.id == uuid.UUID(old["id"])).values(completed_at=(await db_now(session)) - timedelta(days=2))
        )

    asyncio.run(with_session(age))
    _run(fakes)
    assert fakes.for_user(portal.email) == []
    assert _row(queued["id"]).notified_at is None and _row(old["id"]).notified_at is None


def test_a_failed_delivery_is_tried_again_up_to_the_cap(client, fakes, monkeypatch):
    monkeypatch.setattr(settings, "notify_max_tries", 3)
    portal = Portal(client)
    _settings(client, portal, notify_email=False, notify_webhook_url="https://hooks.example/flaky")
    job = _finish(client, portal)
    fakes.fail_hooks = 99
    for expected in (1, 2, 3):
        _run(fakes)
        row = _row(job["id"])
        assert (row.notify_tries, row.notified_at) == (expected, None)
    fakes.fail_hooks = 0
    _run(fakes)  # the cap is reached: no fourth attempt, ever
    assert fakes.hooks_to("https://hooks.example/flaky") == []
    assert _row(job["id"]).notify_tries == 3


def test_a_delivery_that_works_on_the_second_try_is_marked_done(client, fakes):
    portal = Portal(client)
    _settings(client, portal, notify_email=False, notify_webhook_url="https://hooks.example/second")
    job = _finish(client, portal)
    fakes.fail_hooks = 1
    _run(fakes)
    assert _row(job["id"]).notified_at is None
    _run(fakes)
    row = _row(job["id"])
    assert row.notified_at is not None and row.notify_tries == 1
    assert len(fakes.hooks_to("https://hooks.example/second")) == 1


def test_one_unreachable_webhook_does_not_stop_other_users(client, fakes):
    broken, fine = Portal(client), Portal(client)
    _settings(client, broken, notify_email=False, notify_webhook_url="https://hooks.example/broken")
    _finish(client, broken)
    fine_job = _finish(client, fine)
    fakes.fail_hooks = 1  # only the first delivery fails
    _run(fakes)
    assert len(fakes.for_user(fine.email)) == 1
    assert _row(fine_job["id"]).notified_at is not None


def test_a_requeued_job_is_announced_again_when_it_finishes(client, fakes):
    portal = Portal(client)
    job = _finish(client, portal, ok=False)
    _run(fakes)
    assert len(fakes.for_user(portal.email)) == 1
    resp = client.post(f"/admin/jobs/{job['id']}/requeue", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    row = _row(job["id"])
    assert (row.notified_at, row.notify_tries) == (None, 0)


# --- The real senders --------------------------------------------------------------


def test_the_webhook_uses_a_short_timeout_and_fails_on_an_error_status(monkeypatch):
    seen: dict = {}

    async def public(host, port):
        return ["93.184.216.34"]

    monkeypatch.setattr(notify, "resolve_public", public)

    class Client(httpx.AsyncClient):
        def __init__(self, **kwargs):
            seen.update(kwargs)
            super().__init__(transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kwargs)

    monkeypatch.setattr(notify.httpx, "AsyncClient", Client)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(notify.post_webhook("https://hooks.example/x", {"a": 1}))
    assert seen["timeout"] <= 10


def test_the_smtp_sender_logs_in_over_starttls(monkeypatch):
    calls: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"], message["From"], message.get_content().strip()))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    monkeypatch.setattr(settings, "smtp_port", 2525)
    monkeypatch.setattr(settings, "smtp_user", "bot")
    monkeypatch.setattr(settings, "smtp_password", "pw")
    monkeypatch.setattr(settings, "smtp_from", "lab@uni.edu")
    monkeypatch.setattr(settings, "smtp_starttls", True)
    asyncio.run(notify.send_email("a@uni.edu", "Subject", "Body"))
    assert calls == [
        ("connect", "smtp.test", 2525),
        ("starttls",),
        ("login", "bot", "pw"),
        ("send", "a@uni.edu", "Subject", "lab@uni.edu", "Body"),
    ]
