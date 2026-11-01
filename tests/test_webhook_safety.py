"""Webhooks must not reach the lab's own network (X5)."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid

import httpx
import pytest
from sqlalchemy import func, update

from app import notify
from app.config import settings
from app.models import Job, User
from app.notify import JobNotifier, WebhookRefused, address_is_blocked, webhook_problem
from app.scheduler import db_now
from dbhelp import with_session
from harness.jobs import Portal

from test_notifications import _finish, _row

BLOCKED = [
    "127.0.0.1", "127.1.2.3", "::1", "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "fd00::1", "fc00::5", "169.254.169.254", "169.254.0.1", "fe80::1", "224.0.0.1", "ff02::1",
    "0.0.0.0", "::", "::ffff:127.0.0.1", "::ffff:10.1.1.1", "100.64.0.1", "240.0.0.1",
]  # fmt: skip
PUBLIC = ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111", "::ffff:8.8.8.8"]


@pytest.mark.parametrize("address", BLOCKED)
def test_internal_addresses_are_blocked(address):
    assert address_is_blocked(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_are_allowed(address):
    assert not address_is_blocked(ipaddress.ip_address(address))


# --- When the address is saved ----------------------------------------------------


def _put(client, portal, url):
    return client.put("/me/notifications", json={"notify_webhook_url": url}, headers=portal.headers)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8080/hook",
        "https://LOCALHOST/x",
        "http://app.localhost/x",
        "http://localhost./x",
        "http://127.0.0.1:8080/",
        "http://127.1/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://10.0.0.5/hook",
        "http://192.168.0.10/",
        "http://172.20.0.2/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[fd12:3456::1]/",
        "http://[fe80::1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://0.0.0.0/",
        "http://224.0.0.251/",
        "http://user:pw@127.0.0.1/",
    ],
)
def test_saving_a_webhook_to_an_internal_address_is_refused(client, url):
    portal = Portal(client)
    resp = _put(client, portal, url)
    assert resp.status_code == 422, url
    assert client.get("/me/notifications", headers=portal.headers).json()["notify_webhook_url"] is None


def test_a_refused_webhook_changes_nothing_else(client):
    portal = Portal(client)
    resp = client.put(
        "/me/notifications", json={"notify_email": False, "notify_webhook_url": "http://127.0.0.1/"}, headers=portal.headers
    )
    assert resp.status_code == 422
    assert client.get("/me/notifications", headers=portal.headers).json()["notify_email"] is True


@pytest.mark.parametrize("url", ["https://hooks.example/abc", "http://93.184.216.34:8080/x", "https://[2606:4700:4700::1111]/"])
def test_public_webhooks_can_be_saved(client, url):
    assert _put(client, Portal(client), url).status_code == 200


def test_clearing_a_webhook_is_still_allowed(client):
    portal = Portal(client)
    _put(client, portal, "https://hooks.example/abc")
    assert _put(client, portal, "").json()["notify_webhook_url"] is None


def test_the_opt_in_allows_private_webhooks(client, monkeypatch):
    monkeypatch.setattr(settings, "notify_allow_private_webhooks", True)
    portal = Portal(client)
    assert _put(client, portal, "http://10.0.0.5:9000/hook").status_code == 200
    assert webhook_problem("http://localhost/x") is None


# --- When it is sent --------------------------------------------------------------


class Net:
    """A fake resolver and transport; records where requests go."""

    def __init__(self, monkeypatch, answers: dict[str, list[str]], handler=None):
        self.requests: list[httpx.Request] = []
        self.lookups: list[str] = []
        self.handler = handler or (lambda request: httpx.Response(200))

        def getaddrinfo(host, port, *args, **kwargs):
            self.lookups.append(host)
            if host not in answers:
                raise socket.gaierror("unknown host")
            return [(socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port)) for a in answers[host]]

        def transport(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.handler(request)

        real = httpx.AsyncClient

        class Client(real):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(transport), **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
        monkeypatch.setattr(notify.httpx, "AsyncClient", Client)


def _send(url, payload=None):
    return asyncio.run(notify.post_webhook(url, payload or {"a": 1}))


def test_a_name_that_resolves_to_a_private_address_is_not_called(monkeypatch):
    net = Net(monkeypatch, {"evil.example": ["10.0.0.7"]})
    with pytest.raises(WebhookRefused):
        _send("https://evil.example/hook")
    assert net.requests == []


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "fd00::2", "::1", "0.0.0.0"])
def test_loopback_metadata_and_ula_answers_are_refused(monkeypatch, address):
    net = Net(monkeypatch, {"evil.example": [address]})
    with pytest.raises(WebhookRefused):
        _send("http://evil.example/hook")
    assert net.requests == []


def test_one_internal_record_among_public_ones_is_enough_to_refuse(monkeypatch):
    net = Net(monkeypatch, {"mixed.example": ["93.184.216.34", "192.168.1.5"]})
    with pytest.raises(WebhookRefused):
        _send("https://mixed.example/hook")
    assert net.requests == []


def test_a_literal_internal_address_is_refused_without_a_lookup(monkeypatch):
    net = Net(monkeypatch, {})
    for url in ("http://127.0.0.1:9/x", "http://localhost/x", "http://2130706433/"):
        with pytest.raises(WebhookRefused):
            _send(url)
    assert net.lookups == [] and net.requests == []


def test_the_request_goes_to_the_address_that_was_checked(monkeypatch):
    net = Net(monkeypatch, {"hooks.example": ["93.184.216.34"]})
    _send("https://hooks.example:8443/path?x=1")
    [request] = net.requests
    assert net.lookups == ["hooks.example"]
    assert request.url.host == "93.184.216.34" and request.url.port == 8443
    assert request.url.raw_path == b"/path?x=1"
    assert request.headers["Host"] == "hooks.example:8443"
    assert request.extensions["sni_hostname"] == "hooks.example"
    assert request.method == "POST"


def test_an_ipv6_answer_is_bracketed_and_credentials_are_kept(monkeypatch):
    net = Net(monkeypatch, {"hooks.example": ["2606:4700:4700::1111"]})
    _send("http://bot:s3cret@hooks.example/hook")
    [request] = net.requests
    assert request.url.host == "2606:4700:4700::1111" and request.url.port in (None, 80)
    assert request.headers["Host"] == "hooks.example"
    assert request.headers["Authorization"].startswith("Basic ")


def test_a_redirect_is_an_error_and_is_not_followed(monkeypatch):
    net = Net(
        monkeypatch,
        {"hooks.example": ["93.184.216.34"]},
        handler=lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}),
    )
    with pytest.raises(WebhookRefused):
        _send("https://hooks.example/hook")
    assert len(net.requests) == 1


def test_an_error_status_still_fails_the_delivery(monkeypatch):
    Net(monkeypatch, {"hooks.example": ["93.184.216.34"]}, handler=lambda request: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        _send("https://hooks.example/hook")


def test_with_the_opt_in_the_url_is_posted_as_it_is(monkeypatch):
    monkeypatch.setattr(settings, "notify_allow_private_webhooks", True)
    net = Net(monkeypatch, {})
    _send("http://10.1.2.3:9000/hook")
    [request] = net.requests
    assert str(request.url) == "http://10.1.2.3:9000/hook" and net.lookups == []


def test_a_blocked_webhook_counts_as_a_failed_try_and_never_reaches_the_network(client, monkeypatch):
    net = Net(monkeypatch, {})
    portal = Portal(client)
    job = _finish(client, portal)

    async def point_inside(session):
        await session.execute(
            update(User).where(User.email == portal.email).values(notify_email=False, notify_webhook_url="http://127.0.0.1:9/hook")
        )

    async def others_are_done(session):
        # the real notifier looks at every finished job; leave the other tests' alone
        await session.execute(
            update(Job).where(Job.id != uuid.UUID(job["id"]), Job.notified_at.is_(None)).values(notified_at=func.now())
        )

    asyncio.run(with_session(point_inside))
    asyncio.run(with_session(others_are_done))

    async def once(session):
        return await JobNotifier().run_once(session, await db_now(session))

    assert asyncio.run(with_session(once)) == 0
    row = _row(job["id"])
    assert row.notify_tries == 1 and row.notified_at is None
    assert net.requests == []
