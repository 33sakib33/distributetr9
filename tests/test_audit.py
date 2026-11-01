"""Audit log (B21): every admin mutation leaves a row, and GET /admin/audit finds it."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app import platform_settings
from app.alerts import Alert, sync_alerts
from app.audit import ACTIONS
from app.config import settings
from harness.client import FakeAgent
from harness.jobs import Portal
from tests.dbhelp import with_session

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _caps(tag: str) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}


def _node(client, tag: str | None = None) -> FakeAgent:
    tag = tag or uuid.uuid4().hex[:8]
    agent = FakeAgent(client, hostname=f"audit-{tag}", caps=_caps(tag))
    agent.register()
    agent.heartbeat()
    return agent


def _audit(client, **params) -> list[dict]:
    resp = client.get("/admin/audit", params={"limit": 200, **params}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _one(client, action: str, target_id) -> dict:
    rows = _audit(client, action=action, target_id=str(target_id))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["actor_email"] == "admin@uni.edu" and row["actor_user_id"]
    return row


def _post(client, path: str, **kw):
    resp = client.post(path, headers=ADMIN, **kw)
    assert resp.status_code in (200, 201), resp.text
    return resp


NODE_ACTIONS = {
    "node.drain": lambda c, n: _post(c, f"/nodes/{n.node_id}/drain"),
    "node.quarantine": lambda c, n: _post(c, f"/nodes/{n.node_id}/quarantine", json={"reason": "bad fan"}),
    "node.release": lambda c, n: _post(c, f"/nodes/{n.node_id}/release"),
    "node.revoke": lambda c, n: _post(c, f"/nodes/{n.node_id}/revoke"),
    "node.forget": lambda c, n: _post(c, f"/nodes/{n.node_id}/forget"),
    "node.ring": lambda c, n: _post(c, f"/nodes/{n.node_id}/ring", json={"ring": 1}),
    "node.meta": lambda c, n: _post(c, f"/nodes/{n.node_id}/meta", json={"room": "Lab 9", "labels": ["a"]}),
    "node.logs_request": lambda c, n: _post(c, f"/nodes/{n.node_id}/logs/request"),
}


@pytest.mark.parametrize("action", list(NODE_ACTIONS))
def test_every_node_action_writes_an_audit_row(client, action):
    node = _node(client)
    NODE_ACTIONS[action](client, node)
    row = _one(client, action, node.node_id)
    assert row["target_type"] == "node" and row["details"]["hostname"] == node.hostname
    assert datetime.fromisoformat(row["at"]) > datetime.now(timezone.utc) - timedelta(minutes=5)


def test_node_action_details_say_what_changed(client):
    node = _node(client)
    _post(client, f"/nodes/{node.node_id}/ring", json={"ring": 0})
    _post(client, f"/nodes/{node.node_id}/quarantine", json={"reason": "bad fan"})
    _post(client, f"/nodes/{node.node_id}/meta", json={"room": "Lab 9", "labels": ["x", "y"]})
    assert _one(client, "node.ring", node.node_id)["details"] == {"hostname": node.hostname, "ring": 0, "previous": 2}
    assert _one(client, "node.quarantine", node.node_id)["details"]["reason"] == "bad fan"
    meta = _one(client, "node.meta", node.node_id)["details"]
    assert meta["room"] == "Lab 9" and meta["labels"] == ["x", "y"]


def test_a_refused_action_leaves_no_row(client):
    ghost = uuid.uuid4()
    assert client.post(f"/nodes/{ghost}/drain", headers=ADMIN).status_code == 404
    assert _audit(client, target_id=str(ghost)) == []
    revoked = _node(client)
    _post(client, f"/nodes/{revoked.node_id}/revoke")
    assert client.post(f"/nodes/{revoked.node_id}/release", headers=ADMIN).status_code == 409
    assert _audit(client, action="node.release", target_id=revoked.node_id) == []


def test_job_actions_write_audit_rows(client):
    tag = uuid.uuid4().hex[:8]
    owner = Portal(client)
    first = owner.submit_job(gpu_class=tag)
    second = owner.submit_job(gpu_class=tag)
    third = owner.submit_job(gpu_class=tag)

    # an admin cancelling someone else's job is recorded; the owner cancelling their own is not
    assert client.post(f"/jobs/{first['id']}/cancel", headers=ADMIN).status_code == 200
    row = _one(client, "job.cancel", first["id"])
    assert row["target_type"] == "job" and row["details"]["owner_id"] == first["user_id"]
    assert owner.cancel(second["id"])["status_code"] == 200
    assert _audit(client, action="job.cancel", target_id=second["id"]) == []

    bulk = _post(client, "/admin/jobs/cancel", json={"job_ids": [third["id"], str(uuid.uuid4())]}).json()
    assert bulk["cancelled"] == [third["id"]]
    rows = [r for r in _audit(client, action="job.bulk_cancel") if third["id"] in r["details"]["job_ids"]]
    assert len(rows) == 1 and rows[0]["details"]["skipped"] == 1 and rows[0]["target_id"] is None

    _post(client, f"/admin/jobs/{first['id']}/requeue")
    assert _one(client, "job.requeue", first["id"])["details"] == {"was": "cancelled"}


async def _open_alert(client, kind: str = "offline") -> dict:
    alert = Alert(f"{kind}:{uuid.uuid4()}", "warning", f"{kind} test alert")

    async def go(session):
        await sync_alerts(session, [alert], datetime.now(timezone.utc))

    await with_session(go)
    groups = client.get("/admin/alerts", headers=ADMIN).json()["groups"]
    return next(a for g in groups for a in g["alerts"] if a["key"] == alert.key)


async def test_alert_actions_write_audit_rows(client):
    alert = await _open_alert(client)
    _post(client, f"/admin/alerts/{alert['id']}/ack")
    assert _one(client, "alert.ack", alert["id"])["details"]["key"] == alert["key"]

    other = await _open_alert(client)
    _post(client, f"/admin/alerts/{other['id']}/mute", json={"minutes": 15})
    assert _one(client, "alert.mute", other["id"])["details"]["minutes"] == 15

    kind = f"disk{uuid.uuid4().hex[:6]}"
    await _open_alert(client, kind)
    _post(client, "/admin/alerts/ack-kind", json={"kind": kind})
    row = _one(client, "alert.ack_kind", kind)
    assert row["target_type"] == "alert_kind" and row["details"]["acknowledged"] == 1


def test_schedule_changes_write_audit_rows(client):
    room = f"Lab {uuid.uuid4().hex[:6]}"
    made = _post(client, "/admin/schedules", json={"room": room, "weekday": 1, "start_minute": 540, "end_minute": 600}).json()
    row = _one(client, "schedule.create", made["id"])
    assert row["target_type"] == "schedule"
    assert row["details"] == {"room": room, "node": None, "weekday": 1, "start": "09:00", "end": "10:00", "note": None}

    assert client.patch(f"/admin/schedules/{made['id']}", json={"end_minute": 660}, headers=ADMIN).status_code == 200
    assert _one(client, "schedule.update", made["id"])["details"]["end"] == "11:00"
    assert client.delete(f"/admin/schedules/{made['id']}", headers=ADMIN).status_code == 200
    assert _one(client, "schedule.delete", made["id"])["details"]["room"] == room


def test_quota_changes_write_audit_rows(client):
    email = f"quota-{uuid.uuid4().hex[:8]}@uni.edu"
    user_id = client.get("/me", headers={"X-Dev-User": email}).json()["id"]
    resp = client.put(f"/admin/quotas/user/{user_id}", json={"max_concurrent_jobs": 9}, headers=ADMIN)
    assert resp.status_code == 200
    row = _one(client, "quota.set", user_id)
    assert row["target_type"] == "user"
    assert row["details"] == {"label": email, "max_concurrent_jobs": 9, "previous": settings.default_user_concurrency}


def test_block_and_unblock_write_audit_rows(client):
    email = f"blk-{uuid.uuid4().hex[:8]}@uni.edu"
    user_id = client.get("/me", headers={"X-Dev-User": email}).json()["id"]
    _post(client, f"/admin/users/{user_id}/block")
    assert _one(client, "user.block", user_id)["details"] == {"email": email}
    _post(client, f"/admin/users/{user_id}/unblock")
    assert _one(client, "user.unblock", user_id)["details"] == {"email": email}


def test_member_changes_write_audit_rows(client, monkeypatch):
    client.get("/me", headers=ADMIN)
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    owner = {"X-Dev-User": f"own-{uuid.uuid4().hex[:8]}@uni.edu"}
    member = f"mem-{uuid.uuid4().hex[:8]}@uni.edu"
    project = client.post("/projects", json={"name": f"aud-{uuid.uuid4().hex[:6]}"}, headers=owner).json()["id"]

    added = client.post(f"/projects/{project}/members", json={"email": member}, headers=owner)
    assert added.status_code == 201
    user_id = added.json()["user_id"]
    rows = _audit(client, action="member.add", target_id=project)
    assert len(rows) == 1 and rows[0]["details"]["email"] == member
    assert rows[0]["actor_email"] == owner["X-Dev-User"]  # an owner's change is recorded too

    assert client.patch(f"/projects/{project}/members/{user_id}", json={"role": "owner"}, headers=owner).status_code == 200
    changed = _audit(client, action="member.role", target_id=project)
    assert len(changed) == 1 and changed[0]["details"]["previous"] == "member" and changed[0]["details"]["role"] == "owner"

    assert client.delete(f"/projects/{project}/members/{user_id}", headers=ADMIN).status_code == 200
    removed = _audit(client, action="member.remove", target_id=project)
    assert len(removed) == 1 and removed[0]["actor_email"] == "admin@uni.edu" and removed[0]["target_type"] == "project"


def test_catalog_changes_write_audit_rows(client):
    name = f"aud-{uuid.uuid4().hex[:8]}"
    body = {"name": name, "framework": "python", "python_version": "3.12", "cuda_version": "none",
            "gpu_archs": ["cpu"], "image_digest": "python:3.12.7-slim"}
    env = _post(client, "/admin/environments", json=body).json()
    assert _one(client, "environment.create", env["id"])["details"]["name"] == name

    def patch(**changes):
        return client.patch(f"/admin/environments/{env['id']}", json=changes, headers=ADMIN)

    assert patch(framework="jax").status_code == 200
    update = _one(client, "environment.update", env["id"])
    assert update["details"]["changes"] == {"framework": {"from": "python", "to": "jax"}}
    assert patch(retired=True).status_code == 200
    assert _one(client, "environment.retire", env["id"])["details"]["name"] == name
    assert patch(retired=False).status_code == 200
    assert _one(client, "environment.restore", env["id"])
    patch(retired=True)
    assert patch(framework="jax").status_code == 200  # no change: nothing to record
    assert len(_audit(client, action="environment.update", target_id=env["id"])) == 1


def test_rollout_changes_write_an_audit_row(client):
    try:
        resp = client.put("/admin/rollout", json={"rollout_ring": 1, "min_version": "0.1.0"}, headers=ADMIN)
        assert resp.status_code == 200
        rows = [r for r in _audit(client, action="rollout.update") if r["details"]["changes"].get("min_version", {}).get("to") == "0.1.0"]
        assert len(rows) == 1
        assert rows[0]["target_type"] == "rollout" and rows[0]["details"]["changes"]["rollout_ring"]["to"] == 1
    finally:
        client.put("/admin/rollout", json={"rollout_ring": None, "min_version": None}, headers=ADMIN)
        platform_settings.clear_cache()


def test_every_audited_action_is_a_known_name():
    assert len(ACTIONS) == len(set(ACTIONS))
    for expected in (
        "node.drain", "node.quarantine", "node.release", "node.revoke", "node.forget", "node.ring", "node.meta",
        "job.requeue", "job.bulk_cancel", "job.cancel", "alert.ack", "alert.mute", "alert.ack_kind",
        "schedule.create", "quota.set", "user.block", "member.add", "environment.create", "rollout.update",
    ):
        assert expected in ACTIONS


def test_audit_filters_and_paging(client):
    nodes = [_node(client) for _ in range(3)]
    for node in nodes:
        _post(client, f"/nodes/{node.node_id}/drain")
    _post(client, f"/nodes/{nodes[0].node_id}/release")

    drains = _audit(client, action="node.drain")
    assert {r["target_id"] for r in drains} >= {n.node_id for n in nodes}
    assert all(r["action"] == "node.drain" for r in drains)
    family = _audit(client, action="node.*", target_id=nodes[0].node_id)
    assert [r["action"] for r in family] == ["node.release", "node.drain"]  # newest first
    assert _audit(client, actor="ADMIN@uni", target_id=nodes[0].node_id)
    assert _audit(client, actor="nobody-like-this", target_id=nodes[0].node_id) == []
    assert _audit(client, target_type="project", target_id=nodes[0].node_id) == []
    assert _audit(client, target_type="node", target_id=nodes[0].node_id)

    today = datetime.now(timezone.utc).date().isoformat()
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date().isoformat()
    assert _audit(client, since=today, target_id=nodes[0].node_id)
    assert _audit(client, since=tomorrow, target_id=nodes[0].node_id) == []
    assert _audit(client, until=today, target_id=nodes[0].node_id)  # `until` is inclusive of that whole day
    assert _audit(client, until="2000-01-01", target_id=nodes[0].node_id) == []
    assert client.get("/admin/audit", params={"since": "not-a-date"}, headers=ADMIN).status_code == 400

    seen: list[str] = []
    cursor = None
    while True:
        params = {"action": "node.drain", "limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/admin/audit", params=params, headers=ADMIN).json()
        assert len(page["items"]) <= 2
        seen += [r["id"] for r in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == len(_audit(client, action="node.drain"))
    stamps = [r["at"] for r in _audit(client)]
    assert stamps == sorted(stamps, reverse=True)
    assert client.get("/admin/audit", params={"cursor": "junk"}, headers=ADMIN).status_code == 400
    assert client.get("/admin/audit", headers=ADMIN).json()["actions"] == list(ACTIONS)


def test_the_audit_log_is_for_admins(client, monkeypatch):
    client.get("/me", headers=ADMIN)
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/audit", headers=student).status_code == 403
