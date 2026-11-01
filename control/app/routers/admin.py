"""Admin views over jobs (B14) and the alert panel (B15). Every mutation here is audited (B21)."""
from __future__ import annotations

import uuid

from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import exists, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import alerts as alert_service
from app.audit import record_audit
from app.db import get_session
from app.deps import require_admin
from app.envbuild import restart_failed_build
from app.errors import AppError
from app.models import AlertRecord, Job, JobAttempt, Node, Project, User
from app.paging import decode_cursor, encode_cursor
from app.scheduler import db_now, load_job, request_cancel
from app.schemas import AckKindIn, BulkCancelIn, MuteIn
from app.serialize import serialize_job
from app.usage import parse_moment
from app.status import derived_status, latest_attempt

router = APIRouter()

JOB_STATUSES = {
    "queued", "building", "starting", "running", "uploading", "unknown",
    "cancelling", "completed", "failed", "cancelled",
}
_FINAL = {"completed", "failed", "cancelled"}


def _wanted_statuses(raw: str | None) -> set[str]:
    wanted = {s.strip().lower() for s in (raw or "").split(",") if s.strip()}
    unknown = wanted - JOB_STATUSES
    if unknown:
        raise AppError(400, "invalid", f"unknown status: {', '.join(sorted(unknown))}")
    return wanted


@router.get("/admin/jobs")
async def admin_jobs(
    status: str | None = None,
    user: str | None = None,
    project_id: uuid.UUID | None = None,
    gpu_class: str | None = None,
    node: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every user's jobs, newest first. `status` is the derived status (one or
    several, comma separated); `node` matches the hostname of any node that ran
    an attempt of the job."""
    wanted = _wanted_statuses(status)
    stmt = (
        select(Job, User.email, Project.name)
        .join(User, User.id == Job.user_id)
        .join(Project, Project.id == Job.project_id)
        .options(selectinload(Job.attempts))
        .order_by(Job.created_at.desc(), Job.id.desc())
    )
    if user and user.strip():
        stmt = stmt.where(User.email.icontains(user.strip(), autoescape=True))
    if project_id:
        stmt = stmt.where(Job.project_id == project_id)
    if gpu_class:
        stmt = stmt.where(Job.resources["gpu_class"].astext == gpu_class)
    if node and node.strip():
        stmt = stmt.where(
            exists(
                select(JobAttempt.id)
                .join(Node, Node.id == JobAttempt.node_id)
                .where(JobAttempt.job_id == Job.id, Node.hostname.icontains(node.strip(), autoescape=True))
            )
        )
    if since:
        stmt = stmt.where(Job.created_at >= parse_moment(since))
    if until:
        stmt = stmt.where(Job.created_at < parse_moment(until, end=True))
    after = decode_cursor(cursor) if cursor else None

    # The status is derived in Python, so with a status filter scan in batches
    # until a page (plus one, to know whether there is more) has matched.
    batch = max(limit * 4, 200) if wanted else limit + 1
    matches: list[tuple[Job, str, str]] = []
    while len(matches) <= limit:
        page = stmt.limit(batch)
        if after is not None:
            page = page.where(tuple_(Job.created_at, Job.id) < tuple_(*after))
        rows = (await session.execute(page)).all()
        for job, email, project_name in rows:
            if not wanted or derived_status(job) in wanted:
                matches.append((job, email, project_name))
                if len(matches) > limit:
                    break
        if len(rows) < batch:
            break
        after = (rows[-1][0].created_at, rows[-1][0].id)
    items = matches[:limit]

    node_ids = {a.node_id for job, _, _ in items if (a := latest_attempt(job)) is not None}
    nodes = (
        {n.id: n for n in (await session.scalars(select(Node).where(Node.id.in_(node_ids)))).all()}
        if node_ids
        else {}
    )
    out = []
    for job, email, project_name in items:
        attempt = latest_attempt(job)
        payload = serialize_job(job, nodes.get(attempt.node_id) if attempt else None)
        out.append({**payload, "user_email": email, "project_name": project_name})
    next_cursor = None
    if len(matches) > limit:
        last = items[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return {"items": out, "next_cursor": next_cursor}


@router.post("/admin/jobs/cancel")
async def bulk_cancel(
    body: BulkCancelIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Cancel many jobs at once; finished and unknown ids are reported, not fatal."""
    cancelled: list[str] = []
    skipped: list[dict] = []
    for job_id in dict.fromkeys(body.job_ids):
        try:
            job = await load_job(session, job_id)
            if derived_status(job) in _FINAL:
                skipped.append({"job_id": str(job_id), "reason": f"already {derived_status(job)}"})
                continue
            await request_cancel(session, job_id)
            cancelled.append(str(job_id))
        except AppError as exc:
            skipped.append({"job_id": str(job_id), "reason": exc.message})
    record_audit(
        session, admin, "job.bulk_cancel", "job", None,
        {"job_ids": cancelled, "skipped": len(skipped)},
    )
    await session.commit()
    return {"cancelled": cancelled, "skipped": skipped}


@router.post("/admin/jobs/{job_id}/requeue")
async def requeue_job(
    job_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Make a failed or cancelled job runnable again, with three more attempts."""
    from app.serialize import load_job as load_for_response
    from app.serialize import node_for_job

    job = await load_job(session, job_id, lock=True)
    status = derived_status(job)
    if status not in {"failed", "cancelled"}:
        raise AppError(409, "conflict", f"only failed or cancelled jobs can be requeued (this one is {status})")
    attempt = latest_attempt(job)
    job.desired_state = "run"
    job.accepted_attempt_id = None
    job.completed_at = None
    job.notified_at, job.notify_tries = None, 0  # tell the owner again when it finishes
    job.max_attempts = max(job.max_attempts, (attempt.attempt_number if attempt else 0) + 3)
    job.pending_reason = (
        "building environment" if await restart_failed_build(session, job) else "requeued by admin"
    )
    record_audit(session, admin, "job.requeue", "job", job.id, {"was": status})
    await session.commit()
    job = await load_for_response(session, job_id)
    return serialize_job(job, await node_for_job(session, job))


# --- Alerts ---------------------------------------------------------------------


@router.get("/admin/alerts")
async def admin_alerts(
    include_resolved: bool = False,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    now = await db_now(session)
    return {"groups": await alert_service.grouped_alerts(session, now, include_resolved)}


@router.post("/admin/alerts/ack-kind")
async def acknowledge_kind(
    body: AckKindIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    now = await db_now(session)
    result = await session.execute(
        update(AlertRecord)
        .where(
            AlertRecord.kind == body.kind,
            AlertRecord.resolved_at.is_(None),
            AlertRecord.acknowledged_at.is_(None),
        )
        .values(acknowledged_at=now, acknowledged_by=admin.email)
    )
    record_audit(session, admin, "alert.ack_kind", "alert_kind", body.kind, {"acknowledged": result.rowcount})
    await session.commit()
    return {"kind": body.kind, "acknowledged": result.rowcount}


@router.post("/admin/alerts/{alert_id}/ack")
async def acknowledge_alert(
    alert_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await alert_service.get_alert(session, alert_id)
    now = await db_now(session)
    if row.acknowledged_at is None:
        row.acknowledged_at = now
        row.acknowledged_by = admin.email
    record_audit(session, admin, "alert.ack", "alert", row.id, {"key": row.key})
    await session.commit()
    return alert_service.serialize_alert(row, now)


@router.post("/admin/alerts/{alert_id}/mute")
async def mute_alert(
    alert_id: uuid.UUID,
    body: MuteIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await alert_service.get_alert(session, alert_id)
    now = await db_now(session)
    row.muted_until = now + timedelta(minutes=body.minutes)
    record_audit(session, admin, "alert.mute", "alert", row.id, {"key": row.key, "minutes": body.minutes})
    await session.commit()
    return alert_service.serialize_alert(row, now)
