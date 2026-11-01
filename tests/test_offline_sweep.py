"""The offline sweep and heartbeats both change a node's eligibility; neither may
overwrite what the other just did with a decision based on a stale copy."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.models import Node
from app.routers import agent as agent_router
from app.scheduler import mark_offline_nodes
from harness.client import FakeAgent
from tests.dbhelp import with_session


def _node(client) -> FakeAgent:
    agent = FakeAgent(client, hostname=f"sweep-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    return agent


async def _set(agent: FakeAgent, **values) -> None:
    async def go(session):
        await session.execute(update(Node).where(Node.id == uuid.UUID(agent.node_id)).values(**values))

    await with_session(go)


async def _eligibility(agent: FakeAgent) -> str:
    async def go(session):
        return await session.scalar(select(Node.eligibility).where(Node.id == uuid.UUID(agent.node_id)))

    return await with_session(go)


async def _sweep() -> None:
    async def go(session):
        await mark_offline_nodes(session)

    await with_session(go)


async def test_the_sweep_marks_silent_nodes_offline_and_returns_heard_ones(client):
    stale = datetime.now(timezone.utc) - timedelta(hours=1)
    silent, heard, drained, quarantined, forgotten = (_node(client) for _ in range(5))
    await _set(silent, last_heartbeat_at=stale)
    await _set(heard, eligibility="offline")  # marked offline, but its heartbeat is fresh
    await _set(drained, last_heartbeat_at=stale, eligibility="draining")
    await _set(quarantined, last_heartbeat_at=stale, eligibility="quarantined")
    await _set(forgotten, last_heartbeat_at=stale, decommissioned_at=datetime.now(timezone.utc))

    await _sweep()

    assert await _eligibility(silent) == "offline"
    assert await _eligibility(heard) == "eligible"
    assert await _eligibility(drained) == "draining"
    assert await _eligibility(quarantined) == "quarantined"
    assert await _eligibility(forgotten) == "eligible"  # left exactly as it was


async def test_a_heartbeat_undoes_an_offline_mark_made_while_it_was_in_flight(client, monkeypatch):
    """The sweep commits between the heartbeat loading its node and saving it."""
    agent = _node(client)
    original = agent_router.record_health

    async def sweep_first(session, *args, **kwargs):
        await _mark_offline_through_the_app(agent.node_id)
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(agent_router, "record_health", sweep_first)
    agent.heartbeat()
    monkeypatch.setattr(agent_router, "record_health", original)

    assert await _eligibility(agent) == "eligible"


async def _mark_offline_through_the_app(node_id: str) -> None:
    from app.db import SessionLocal

    async with SessionLocal() as other:
        await other.execute(update(Node).where(Node.id == uuid.UUID(node_id)).values(eligibility="offline"))
        await other.commit()
