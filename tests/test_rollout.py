"""Rollout settings in the database (B20): overrides on top of the environment
defaults, read through a 10 s cache that PUT (and the tests) clear."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app import agent_bundle, platform_settings
from app.config import settings
from app.models import PlatformSetting
from harness.client import FakeAgent
from harness.jobs import Portal
from tests.dbhelp import with_session

ADMIN = {"X-Dev-User": "admin@uni.edu"}


@pytest.fixture(autouse=True)
def clean_rollout(client):
    yield
    client.put("/admin/rollout", json={"target_version": None, "rollout_ring": None, "min_version": None}, headers=ADMIN)
    platform_settings.clear_cache()


def _put(client, **body):
    return client.put("/admin/rollout", json=body, headers=ADMIN)


def _old_agent(client, tag: str, version: str = "0.0.1") -> FakeAgent:
    caps = {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0,
            "agent_version": version}
    agent = FakeAgent(client, hostname=f"roll-{tag}", caps=caps)
    agent.register()
    return agent


def test_rollout_defaults_come_from_the_environment(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_rollout_ring", 1)
    monkeypatch.setattr(settings, "agent_min_version", "0.3.0")
    platform_settings.clear_cache()
    view = client.get("/admin/rollout", headers=ADMIN).json()
    assert view["rollout_ring"] == 1 and view["min_version"] == "0.3.0"
    assert view["overrides"] == {"agent_target_version": None, "agent_rollout_ring": None, "agent_min_version": None}
    assert view["defaults"]["rollout_ring"] == 1
    bundle = agent_bundle.current_bundle()
    assert view["bundle_version"] == bundle.version and view["target_version"] == bundle.version
    assert "rejected" not in view


def test_put_changes_take_effect_at_once_and_clearing_restores_the_defaults(client):
    tag = uuid.uuid4().hex[:8]
    agent = _old_agent(client, tag)
    assert agent.heartbeat()["agent_update"] is not None  # ring 2 default: everyone

    resp = _put(client, rollout_ring=0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["rollout_ring"] == 0 and resp.json()["overrides"]["agent_rollout_ring"] == 0
    assert agent.heartbeat()["agent_update"] is None  # no waiting for the cache

    assert client.post(f"/nodes/{agent.node_id}/ring", json={"ring": 0}, headers=ADMIN).status_code == 200
    assert agent.heartbeat()["agent_update"] is not None

    cleared = _put(client, rollout_ring=None)
    assert cleared.json()["rollout_ring"] == settings.agent_rollout_ring
    assert cleared.json()["overrides"]["agent_rollout_ring"] is None


def test_a_target_version_that_is_not_the_bundle_stops_updates(client):
    tag = uuid.uuid4().hex[:8]
    agent = _old_agent(client, tag)
    resp = _put(client, target_version="9.9.9")
    assert resp.json()["target_version"] == "9.9.9"
    assert resp.json()["bundle_version"] == agent_bundle.current_bundle().version
    assert agent.heartbeat()["agent_update"] is None  # the server has no 9.9.9 bundle to offer
    assert client.get("/nodes", headers=ADMIN).json()["agent_target_version"] == "9.9.9"

    _put(client, target_version=agent_bundle.current_bundle().version)
    assert agent.heartbeat()["agent_update"] is not None


def test_min_version_override_stops_an_outdated_agent_claiming(client):
    tag = f"min-{uuid.uuid4().hex[:8]}"
    job = Portal(client).submit_job(gpu_class=tag)
    old = _old_agent(client, tag, version="0.4.9")
    old.heartbeat()
    attempt = old.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]  # no minimum yet
    old.run_to_complete(attempt)

    again = Portal(client).submit_job(gpu_class=tag)
    assert _put(client, min_version="0.5.0").json()["min_version"] == "0.5.0"
    assert old.claim() is None
    assert Portal(client).get_job(again["id"])["status"] == "queued"
    assert _put(client, min_version="").json()["min_version"] == (settings.agent_min_version or None)
    assert old.claim() is not None


def test_put_validates_and_needs_something_to_change(client):
    assert _put(client).status_code == 400
    assert _put(client, target_version="latest").status_code == 400
    assert _put(client, min_version="1.x").status_code == 400
    assert _put(client, rollout_ring=10).status_code == 422
    assert client.get("/admin/rollout", headers=ADMIN).json()["overrides"]["agent_target_version"] is None


async def test_the_cache_serves_for_ten_seconds_and_clear_cache_drops_it(client):
    async def write(session):
        session.add(PlatformSetting(key="agent_rollout_ring", value=0, updated_by="test"))

    async def read(session):
        await platform_settings.ensure_loaded(session)
        return platform_settings.rollout_ring()

    platform_settings.clear_cache()
    assert await with_session(read) == settings.agent_rollout_ring
    await with_session(write)
    assert await with_session(read) == settings.agent_rollout_ring  # still cached
    platform_settings.clear_cache()
    assert await with_session(read) == 0

    async def drop(session):
        row = await session.scalar(select(PlatformSetting).where(PlatformSetting.key == "agent_rollout_ring"))
        await session.delete(row)

    await with_session(drop)


def test_nodes_by_version_counts_the_fleet(client):
    tag = uuid.uuid4().hex[:8]
    _old_agent(client, tag, version="7.7.7").heartbeat()
    view = client.get("/admin/rollout", headers=ADMIN).json()
    assert view["nodes_by_version"]["7.7.7"] >= 1


def test_rollout_is_for_admins(client, monkeypatch):
    client.get("/me", headers=ADMIN)  # the admin must exist before new users stop being admins
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/rollout", headers=student).status_code == 403
    assert client.put("/admin/rollout", json={"rollout_ring": 0}, headers=student).status_code == 403
