from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import ACTIVE_ATTEMPT_STATES
from app.models import Artifact, Job, JobAttempt


async def assert_one_accepted_result(session: AsyncSession, job_id) -> None:
    job = await session.get(Job, job_id)
    assert job is not None
    completed = (
        await session.scalars(
            select(JobAttempt).where(
                JobAttempt.job_id == job_id,
                JobAttempt.observed_state == "completed",
            )
        )
    ).all()
    if job.accepted_attempt_id:
        accepted = await session.get(JobAttempt, job.accepted_attempt_id)
        assert accepted is not None
        assert accepted.observed_state in {"completed", "failed", "cancelled"}
        if accepted.observed_state == "completed":
            assert len(completed) == 1
            assert completed[0].id == accepted.id


async def assert_one_active_per_node(session: AsyncSession) -> None:
    rows = (
        await session.execute(
            select(JobAttempt.node_id, func.count())
            .where(JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
            .group_by(JobAttempt.node_id)
        )
    ).all()
    for node_id, count in rows:
        assert count <= 1, f"node {node_id} has {count} active attempts"


async def assert_no_stale_promotion(session: AsyncSession, job_id, winner_attempt_id) -> None:
    keys = (
        await session.scalars(
            select(Artifact.object_key).where(
                Artifact.job_id == job_id,
                Artifact.type == "output",
            )
        )
    ).all()
    prefix = f"jobs/{job_id}/promoted/"
    for key in keys:
        assert key.startswith(prefix)
    if keys:
        winner = await session.get(JobAttempt, winner_attempt_id)
        assert winner is not None
        assert winner.observed_state == "completed"
