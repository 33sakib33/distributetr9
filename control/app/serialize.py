from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_session
from app.deps import get_job_for_user
from app.errors import AppError
from app.models import Job, JobAttempt, Node
from app.status import derived_status, latest_attempt, timings
from app.storage import storage

router = APIRouter()


def serialize_attempt(attempt: JobAttempt | None, node: Node | None = None) -> dict | None:
    if attempt is None:
        return None
    payload = {
        "id": str(attempt.id),
        "attempt_number": attempt.attempt_number,
        "node_id": str(attempt.node_id),
        "observed_state": attempt.observed_state,
        "exit_code": attempt.exit_code,
        "failure_category": attempt.failure_category,
        "lease_expires_at": attempt.lease_expires_at.isoformat() if attempt.lease_expires_at else None,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "completed_at": attempt.completed_at.isoformat() if attempt.completed_at else None,
        "last_log_seq": attempt.last_log_seq,
    }
    if node is not None:
        payload["hostname"] = node.hostname
        payload["compatibility_class"] = node.compatibility_class
    return payload


def serialize_job(job: Job, node: Node | None = None) -> dict:
    attempt = latest_attempt(job)
    now = datetime.now(timezone.utc)
    return {
        "id": str(job.id),
        "user_id": str(job.user_id),
        "project_id": str(job.project_id),
        "entrypoint": job.entrypoint,
        "args": job.args,
        "environment_id": str(job.environment_id),
        "resources": job.resources,
        "desired_state": job.desired_state,
        "status": derived_status(job),
        "pending_reason": job.pending_reason,
        "max_attempts": job.max_attempts,
        "accepted_attempt_id": str(job.accepted_attempt_id) if job.accepted_attempt_id else None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "timings": timings(job, now),
        "latest_attempt": serialize_attempt(attempt, node),
    }


async def node_for_job(session: AsyncSession, job: Job) -> Node | None:
    attempt = latest_attempt(job)
    if attempt is None:
        return None
    return await session.get(Node, attempt.node_id)


async def load_job(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await session.scalar(
        select(Job).where(Job.id == job_id).options(selectinload(Job.attempts))
    )
    if job is None:
        raise AppError(404, "not_found", "job not found", job_id=str(job_id))
    return job
