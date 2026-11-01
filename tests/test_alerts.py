"""Alert lifecycle (B15): the table behind GET /admin/alerts, the worker's
sync, acknowledge / mute / group acknowledge, and what the notifier announces."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.alerts import Alert, Notifier, check, run_cycle, sync_alerts
from app.config import settings
from app.models import AlertRecord, Node
from harness.client import FakeAgent
from tests.dbhelp import with_session

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _offline(node_id: str | None = None, message: str = "pc offline") -> Alert:
    return Alert(f"offline:{node_id or uuid.uuid4()}", "warning", message)


async def _sync(*alerts: Alert, now: datetime | None = None) -> None:
    async def go(session):
        await sync_alerts(session, list(alerts), now or _now())

    await with_session(go)


async def _row(key: str) -> AlertRecord:
    async def go(session):
        return await session.scalar(select(AlertRecord).where(AlertRecord.key == key))

    return await with_session(go)


def _groups(client, **params) -> dict[str, dict]:
    resp = client.get("/admin/alerts", params=params, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {group["kind"]: group for group in resp.json()["groups"]}


def _by_key(group: dict) -> dict[str, dict]:
    return {alert["key"]: alert for alert in group["alerts"]}


async def test_an_alert_opens_resolves_and_reopens(client):
    first = _offline(message="lab-01 offline since 10:00")
    t0 = _now()
    await _sync(first, now=t0)
    row = await _row(first.key)
    assert row.kind == "offline" and row.resolved_at is None
    assert row.first_seen == row.last_seen == t0

    await _sync(Alert(first.key, "warning", "lab-01 offline since 10:00 (still)"), now=t0 + timedelta(minutes=1))
    row = await _row(first.key)
    assert row.first_seen == t0 and row.last_seen == t0 + timedelta(minutes=1)
    assert row.message.endswith("(still)")

    await _sync(now=t0 + timedelta(minutes=2))  # it is gone
    assert (await _row(first.key)).resolved_at == t0 + timedelta(minutes=2)

    await _sync(first, now=t0 + timedelta(minutes=3))  # and back
    row = await _row(first.key)
    assert row.resolved_at is None
    assert row.first_seen == t0 + timedelta(minutes=3)


async def test_a_returning_alert_needs_a_new_acknowledgement_but_keeps_its_mute(client):
    alert = _offline()
    t0 = _now()
    await _sync(alert, now=t0)
    listed = _by_key(_groups(client)["offline"])[alert.key]
    assert client.post(f"/admin/alerts/{listed['id']}/ack", headers=ADMIN).status_code == 200
    assert client.post(f"/admin/alerts/{listed['id']}/mute", json={"minutes": 600}, headers=ADMIN).status_code == 200

    await _sync(now=t0 + timedelta(minutes=1))
    await _sync(alert, now=t0 + timedelta(minutes=2))
    row = await _row(alert.key)
    assert row.resolved_at is None and row.acknowledged_at is None and row.acknowledged_by is None
    assert row.notified_at is None
    assert row.muted_until is not None


async def test_the_endpoint_groups_open_alerts_by_kind_worst_first(client):
    a, b = _offline(message="a offline"), _offline(message="b offline")
    disk = Alert(f"disk:{uuid.uuid4()}", "critical", "c disk full")
    lost = Alert("lease_losses", "warning", "6 attempts lost their node")
    await _sync(a, b, disk, lost)  # also resolves everything an earlier test left open

    groups = _groups(client)
    assert list(groups) == ["disk", "lease_losses", "offline"]  # critical first, then by name
    assert groups["offline"]["count"] == 2 and groups["offline"]["unhandled"] == 2
    assert groups["offline"]["severity"] == "warning"
    assert groups["disk"]["severity"] == "critical"
    assert set(_by_key(groups["offline"])) == {a.key, b.key}
    item = _by_key(groups["disk"])[disk.key]
    assert item["status"] == "open" and item["node_id"] == disk.key.split(":")[1]
    assert set(item) >= {
        "id", "key", "kind", "severity", "message", "status", "first_seen", "last_seen",
        "resolved_at", "acknowledged_at", "acknowledged_by", "muted_until",
    }
    assert _by_key(groups["lease_losses"])["lease_losses"]["node_id"] is None


async def test_resolved_alerts_show_only_on_request(client):
    gone, here = _offline(message="gone"), _offline(message="here")
    await _sync(gone, here)
    await _sync(here)

    assert set(_by_key(_groups(client)["offline"])) == {here.key}
    with_resolved = _groups(client, include_resolved=1)["offline"]
    assert with_resolved["count"] == 1  # the count is of open alerts
    listed = _by_key(with_resolved)
    assert listed[gone.key]["status"] == "resolved" and listed[gone.key]["resolved_at"]
    assert listed[here.key]["status"] == "open"
    assert list(listed)[0] == here.key  # open ones first


async def test_acknowledge_and_mute_one_alert(client):
    keep, quiet, seen = _offline(), _offline(), _offline()
    await _sync(keep, quiet, seen)
    listed = _by_key(_groups(client)["offline"])

    acked = client.post(f"/admin/alerts/{listed[seen.key]['id']}/ack", headers=ADMIN)
    assert acked.status_code == 200
    assert acked.json()["status"] == "acknowledged" and acked.json()["acknowledged_by"] == "admin@uni.edu"
    muted = client.post(f"/admin/alerts/{listed[quiet.key]['id']}/mute", json={"minutes": 15}, headers=ADMIN)
    assert muted.status_code == 200 and muted.json()["status"] == "muted"
    until = datetime.fromisoformat(muted.json()["muted_until"])
    assert timedelta(minutes=14) < until - _now() < timedelta(minutes=16)

    group = _groups(client)["offline"]
    assert group["count"] == 3 and group["unhandled"] == 1  # still open, but two are handled
    status = {k: a["status"] for k, a in _by_key(group).items()}
    assert status == {keep.key: "open", quiet.key: "muted", seen.key: "acknowledged"}


def test_acknowledge_and_mute_reject_bad_requests(client):
    ghost = uuid.uuid4()
    assert client.post(f"/admin/alerts/{ghost}/ack", headers=ADMIN).status_code == 404
    assert client.post(f"/admin/alerts/{ghost}/mute", json={"minutes": 5}, headers=ADMIN).status_code == 404
    assert client.post(f"/admin/alerts/{ghost}/mute", json={"minutes": 0}, headers=ADMIN).status_code == 422
    assert client.post(f"/admin/alerts/{ghost}/mute", json={}, headers=ADMIN).status_code == 422
    assert client.post("/admin/alerts/ack-kind", json={"kind": ""}, headers=ADMIN).status_code == 422


async def test_acknowledging_a_kind_covers_every_open_alert_of_it(client):
    a, b = _offline(), _offline()
    disk = Alert(f"disk:{uuid.uuid4()}", "warning", "disk")
    await _sync(a, b, disk)

    resp = client.post("/admin/alerts/ack-kind", json={"kind": "offline"}, headers=ADMIN)
    assert resp.status_code == 200 and resp.json() == {"kind": "offline", "acknowledged": 2}
    groups = _groups(client)
    assert {x["status"] for x in groups["offline"]["alerts"]} == {"acknowledged"}
    assert groups["offline"]["unhandled"] == 0
    assert groups["disk"]["unhandled"] == 1
    assert client.post("/admin/alerts/ack-kind", json={"kind": "offline"}, headers=ADMIN).json()["acknowledged"] == 0


def test_only_admins_see_alerts(client, monkeypatch):
    client.get("/me", headers=ADMIN)
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/alerts", headers=student).status_code == 403
    assert client.post("/admin/alerts/ack-kind", json={"kind": "offline"}, headers=student).status_code == 403
    assert client.post(f"/admin/alerts/{uuid.uuid4()}/ack", headers=student).status_code == 403


# --- Notifier ---------------------------------------------------------------------


class Sink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    async def __call__(self, url: str, payload: dict) -> None:
        self.texts.append(payload["text"])


async def _announce(sink: Sink, now: datetime) -> list[str]:
    async def go(session):
        return [row.key for row in await Notifier(sink).send_new(session, now)]

    return await with_session(go)


async def test_the_notifier_announces_only_new_unmuted_unacknowledged_alerts(client, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "http://hooks.test/alerts")
    monkeypatch.setattr(settings, "alert_cooldown_seconds", 3600)
    fresh, acked, muted = _offline(message="fresh"), _offline(message="acked"), _offline(message="muted")
    t0 = _now()
    await _sync(fresh, acked, muted, now=t0)
    ids = {k: a["id"] for k, a in _by_key(_groups(client)["offline"]).items()}
    client.post(f"/admin/alerts/{ids[acked.key]}/ack", headers=ADMIN)
    client.post(f"/admin/alerts/{ids[muted.key]}/mute", json={"minutes": 30}, headers=ADMIN)

    sink = Sink()
    assert await _announce(sink, t0) == [fresh.key]
    assert sink.texts == ["Lab platform alerts:\n[warning] fresh"]

    # announced once, not again on the next pass
    assert await _announce(sink, t0 + timedelta(minutes=5)) == []
    # the mute runs out while the alert is still open: now it is news
    assert await _announce(sink, t0 + timedelta(minutes=31)) == [muted.key]
    # an alert that is still open is repeated once the cooldown has passed, unless acknowledged
    assert await _announce(sink, t0 + timedelta(minutes=70)) == [fresh.key]
    assert await _announce(sink, t0 + timedelta(minutes=95)) == [muted.key]


async def test_a_resolved_alert_is_not_announced_and_a_returning_one_is(client, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "http://hooks.test/alerts")
    alert = _offline(message="flapping")
    t0 = _now()
    await _sync(alert, now=t0)
    sink = Sink()
    assert await _announce(sink, t0) == [alert.key]

    await _sync(now=t0 + timedelta(minutes=1))
    assert await _announce(sink, t0 + timedelta(minutes=2)) == []
    await _sync(alert, now=t0 + timedelta(minutes=3))
    assert await _announce(sink, t0 + timedelta(minutes=4)) == [alert.key]


async def test_nothing_is_marked_announced_without_a_webhook_or_when_it_fails(client, monkeypatch):
    import httpx

    alert = _offline()
    await _sync(alert)
    monkeypatch.setattr(settings, "alert_webhook_url", "")
    assert await _announce(Sink(), _now()) == []
    assert (await _row(alert.key)).notified_at is None

    monkeypatch.setattr(settings, "alert_webhook_url", "http://hooks.test/alerts")

    async def broken(url: str, payload: dict) -> None:
        raise httpx.ConnectError("down")

    async def go(session):
        return await Notifier(broken).send_new(session, _now())

    assert await with_session(go) == []
    assert (await _row(alert.key)).notified_at is None  # tried again next time
    assert await _announce(Sink(), _now()) == [alert.key]


# --- The worker's cycle over real data ------------------------------------------------


async def _age_heartbeat(node_id: str, hours: float) -> None:
    async def go(session):
        await session.execute(
            update(Node).where(Node.id == uuid.UUID(node_id)).values(last_heartbeat_at=_now() - timedelta(hours=hours))
        )

    await with_session(go)


async def test_a_cycle_turns_an_offline_pc_into_a_table_alert_and_announces_it(client, monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "http://hooks.test/alerts")
    host = f"cyc-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(client, hostname=host)
    agent.register()
    agent.heartbeat()
    await _age_heartbeat(agent.node_id, 1)
    sink = Sink()

    async def cycle(session):
        return [row.key for row in await run_cycle(session, Notifier(sink))]

    sent = await with_session(cycle)
    key = f"offline:{agent.node_id}"
    assert key in sent
    assert any(host in text for text in sink.texts)
    listed = _by_key(_groups(client)["offline"])[key]
    assert listed["node_id"] == agent.node_id and host in listed["message"]

    assert key not in await with_session(cycle)  # not announced twice

    agent.heartbeat()  # it comes back
    await with_session(cycle)
    assert key not in _by_key(_groups(client).get("offline", {"alerts": []}))
    assert _by_key(_groups(client, include_resolved=1)["offline"])[key]["status"] == "resolved"


async def test_a_forgotten_pc_raises_no_alerts(client):
    agent = FakeAgent(client, hostname=f"gone-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    await _age_heartbeat(agent.node_id, 1)

    async def keys(session):
        return {alert.key for alert in await check(session)}

    assert f"offline:{agent.node_id}" in await with_session(keys)
    assert client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN).status_code == 200
    assert f"offline:{agent.node_id}" not in await with_session(keys)
