"""Rate limits (B29): the token bucket, and the endpoints that use it."""
from __future__ import annotations

import io
import json
import tarfile
import uuid

import pytest

from app import ratelimit
from app.config import settings
from app.ratelimit import TokenBuckets
from harness.jobs import Portal, make_archive


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --- The bucket --------------------------------------------------------------------


def test_a_bucket_allows_a_burst_then_spaces_requests_out():
    clock = Clock()
    buckets = TokenBuckets(clock)
    assert [buckets.take("jobs", "u", 30) for _ in range(30)] == [0] * 30
    assert buckets.take("jobs", "u", 30) == 2  # 30 a minute = one token every 2 seconds
    clock.now += 1
    assert buckets.take("jobs", "u", 30) == 1  # half a token earned
    clock.now += 1
    assert buckets.take("jobs", "u", 30) == 0
    assert buckets.take("jobs", "u", 30) == 2


def test_buckets_are_kept_apart_by_name_and_key_and_refill_to_the_cap():
    clock = Clock()
    buckets = TokenBuckets(clock)
    assert [buckets.take("a", "x", 2) for _ in range(3)] == [0, 0, 30]
    assert buckets.take("a", "y", 2) == 0  # another caller
    assert buckets.take("b", "x", 2) == 0  # another limit
    clock.now += 3600
    assert [buckets.take("a", "x", 2) for _ in range(3)] == [0, 0, 30]  # refilled to 2, not beyond


def test_idle_buckets_are_forgotten_when_the_table_grows(monkeypatch):
    clock = Clock()
    buckets = TokenBuckets(clock)
    monkeypatch.setattr(ratelimit, "MAX_BUCKETS", 5)
    for n in range(6):
        buckets.take("a", str(n), 10)
    clock.now += 300
    buckets.take("a", "fresh", 10)
    assert len(buckets._buckets) == 1


# --- The endpoints -----------------------------------------------------------------


@pytest.fixture(autouse=True)
def limits_on(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    ratelimit.buckets.clear()
    yield
    ratelimit.buckets.clear()
    client.cookies.clear()  # /auth/local signs the shared client in with a cookie


def _submit(client, portal: Portal, project_id: str):
    env = portal.environment_id()
    return client.post(
        "/jobs",
        data={
            "project_id": project_id,
            "entrypoint": "train.py",
            "environment_id": env,
            "args": "[]",
            "resources": json.dumps({"gpu_class": f"rl-{uuid.uuid4().hex[:8]}"}),
        },
        files={"archive": ("a.tar.gz", make_archive(), "application/gzip")},
        headers=portal.headers,
    )


def _assert_throttled(resp):
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limited"
    wait = int(resp.headers["Retry-After"])
    assert wait >= 1 and body["retry_after_seconds"] == wait


def test_submitting_jobs_is_limited_per_user(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_jobs_per_minute", 3)
    busy, other = Portal(client), Portal(client)
    busy_project, other_project = busy.ensure_project(), other.ensure_project()
    assert [_submit(client, busy, busy_project).status_code for _ in range(3)] == [201, 201, 201]
    _assert_throttled(_submit(client, busy, busy_project))
    assert _submit(client, other, other_project).status_code == 201  # not shared between users


def test_inspecting_uploads_is_limited_per_user(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_inspect_per_minute", 2)
    portal = Portal(client)

    def inspect():
        return client.post(
            "/jobs/inspect", files={"archive": ("a.tar.gz", make_archive(), "application/gzip")}, headers=portal.headers
        )

    assert [inspect().status_code for _ in range(2)] == [200, 200]
    _assert_throttled(inspect())
    # the two limits are separate: submitting is not affected
    assert _submit(client, portal, portal.ensure_project()).status_code == 201


def test_a_request_that_is_not_signed_in_is_refused_before_it_is_counted(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_inspect_per_minute", 1)
    for _ in range(3):
        resp = client.post("/jobs/inspect", files={"archive": ("a.tar.gz", make_archive(), "application/gzip")})
        assert resp.status_code == 401


def test_local_login_is_limited_per_address(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 3)
    attempt = lambda: client.post("/auth/local", json={"email": f"rl-{uuid.uuid4().hex[:6]}@uni.edu"})  # noqa: E731
    assert [attempt().status_code for _ in range(3)] == [200, 200, 200]
    _assert_throttled(attempt())


def test_campus_login_shares_the_login_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 2)
    codes = [client.get("/auth/login", follow_redirects=False).status_code for _ in range(2)]
    assert codes == [302, 302]  # redirected on to the portal's login page with a message, not throttled
    _assert_throttled(client.get("/auth/login", follow_redirects=False))
    _assert_throttled(client.post("/auth/local", json={"email": "x@uni.edu"}))


def test_client_errors_are_limited_per_user_and_per_address(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_client_errors_per_minute", 3)
    report = lambda headers=None: client.post("/client-errors", json={"message": "boom"}, headers=headers or {})  # noqa: E731
    assert [report().status_code for _ in range(3)] == [202, 202, 202]
    _assert_throttled(report())
    student = {"X-Dev-User": f"rl-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert [report(student).status_code for _ in range(3)] == [202, 202, 202]  # signed in: counted as themselves
    _assert_throttled(report(student))


def test_limits_can_be_switched_off_and_a_zero_limit_means_none(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_client_errors_per_minute", 1)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    assert [client.post("/client-errors", json={}).status_code for _ in range(5)] == [202] * 5
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_client_errors_per_minute", 0)
    assert [client.post("/client-errors", json={}).status_code for _ in range(5)] == [202] * 5


def test_the_archive_helper_still_builds_a_tar(client):
    # guards the helper the tests above rely on
    with tarfile.open(fileobj=io.BytesIO(make_archive()), mode="r:gz") as tf:
        assert tf.getnames() == ["train.py"]
