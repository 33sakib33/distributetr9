"""X6: a test that leaves an environment build unfinished must not hand it to the
next test's builder (conftest's no_leftover_builds fails such builds)."""
from __future__ import annotations

import uuid

from dbhelp import with_session
from harness.client import FakeAgent
from harness.jobs import Portal
from sqlalchemy import func, select

from app.models import EnvironmentBuild


def _unfinished() -> int:
    async def count(session):
        return await session.scalar(
            select(func.count()).select_from(EnvironmentBuild).where(EnvironmentBuild.state.in_(("pending", "building", "testing")))
        )

    import asyncio

    return asyncio.run(with_session(count))


def test_a_test_can_leave_a_build_pending(client):
    Portal(client).submit_job(gpu_class=f"iso-{uuid.uuid4().hex[:8]}", requirements=f"pkg-{uuid.uuid4().hex[:10]}==1.0\n")
    assert _unfinished() == 1


def test_the_next_test_finds_no_pending_build(client):
    assert _unfinished() == 0
    builder = FakeAgent(
        client,
        hostname=f"iso-b-{uuid.uuid4().hex[:8]}",
        caps={"compatibility_class": "cpu", "gpu_model": "none", "vram_total_mb": 0, "vram_allocatable_mb": 0},
        role="build",
    )
    builder.register()
    builder.heartbeat()
    assert client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"] is None
