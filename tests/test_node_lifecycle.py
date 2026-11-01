"""Forgetting a PC (B15): it leaves the fleet, the scheduler, alerts and metrics."""
from __future__ import annotations

import uuid
from datetime import datetime

from harness.client import FakeAgent
from harness.jobs import Portal
from tests.test_metrics import value

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _caps(tag: str) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}


def _node(client, tag: str, **kw) -> FakeAgent:
    agent = FakeAgent(client, hostname=f"life-{tag}", caps=_caps(tag), **kw)
    agent.register()
    agent.heartbeat()
    return agent


def _hosts(client, **params) -> dict[str, dict]:
    resp = client.get("/nodes", params=params, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {n["hostname"]: n for n in resp.json()["nodes"]}


def test_forget_revokes_the_credential_and_hides_the_node(client):
    tag = uuid.uuid4().hex[:8]
    agent = _node(client, tag)
    assert agent.hostname in _hosts(client)

    resp = client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == agent.node_id
    stamp = datetime.fromisoformat(resp.json()["decommissioned_at"])

    assert agent.hostname not in _hosts(client)
    shown = _hosts(client, include_decommissioned=1)[agent.hostname]
    assert datetime.fromisoformat(shown["decommissioned_at"]) == stamp
    assert shown["eligibility"] == "quarantined"
    assert client.post("/agent/claim", json={}, headers=agent._headers()).status_code == 401
    assert client.post("/agent/heartbeat", json={}, headers=agent._headers()).status_code == 401
    # the history stays reachable
    assert client.get(f"/nodes/{agent.node_id}", headers=ADMIN).json()["decommissioned_at"]

    again = client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN)  # forgetting twice is harmless
    assert again.status_code == 200 and datetime.fromisoformat(again.json()["decommissioned_at"]) == stamp
    assert client.post(f"/nodes/{agent.node_id}/release", headers=ADMIN).status_code == 409


def test_forget_needs_a_known_node_and_an_admin(client, monkeypatch):
    from app.config import settings

    assert client.post(f"/nodes/{uuid.uuid4()}/forget", headers=ADMIN).status_code == 404
    agent = _node(client, uuid.uuid4().hex[:8])
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.post(f"/nodes/{agent.node_id}/forget", headers=student).status_code == 403
    assert agent.hostname in _hosts(client)


def test_forgetting_a_busy_node_fails_its_attempt_and_the_job_goes_elsewhere(client):
    tag = uuid.uuid4().hex[:8]
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    lost = _node(client, tag)
    attempt = lost.claim()
    assert attempt is not None
    lost.acknowledge(attempt)
    lost.event(attempt, "running")

    assert client.post(f"/nodes/{lost.node_id}/forget", headers=ADMIN).status_code == 200
    first = portal.get_job(job["id"])
    assert first["status"] == "queued"
    assert first["latest_attempt"]["failure_category"] == "node_lost"

    spare = FakeAgent(client, hostname=f"life-spare-{tag}", caps=_caps(tag))
    spare.register()
    spare.heartbeat()
    retried = spare.claim()
    assert retried is not None and retried["job_id"] == job["id"]


def test_a_forgotten_node_is_not_counted_in_the_fleet_gauges(client):
    agent = _node(client, uuid.uuid4().hex[:8])
    idle = value(client, "lab_nodes", state="idle")
    client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN)
    assert value(client, "lab_nodes", state="idle") == idle - 1


def test_a_forgotten_node_does_not_change_why_a_job_waits(client):
    tag = uuid.uuid4().hex[:8]
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    agent = _node(client, tag)
    assert portal.get_job(job["id"])["pending_reason"] != f"waiting for node class {tag}"
    client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN)
    assert portal.get_job(job["id"])["pending_reason"] == f"waiting for node class {tag}"


def test_the_scheduler_refuses_a_decommissioned_node_whatever_its_credential():
    """Defence in depth: the revoked credential already stops it; node_can_claim says why too."""
    from datetime import timedelta, timezone
    from types import SimpleNamespace

    from app.scheduler import node_can_claim

    now = datetime.now(timezone.utc)
    node = SimpleNamespace(
        eligibility="eligible", last_heartbeat_at=now - timedelta(seconds=1), decommissioned_at=None,
        gpu_desktop_busy=False, last_health=None, disk_free_bytes=10**12, agent_version="0.0.0",
    )
    assert node_can_claim(node, now) is None
    node.decommissioned_at = now
    assert node_can_claim(node, now) == "decommissioned"
