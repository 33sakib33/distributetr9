from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.agent_bundle import is_outdated
from app.config import settings
from app.enums import (
    ACTIVE_ATTEMPT_STATES,
    FORWARD_TRANSITIONS,
    RETRYABLE_FAILURES,
    TERMINAL_ATTEMPT_STATES,
)
from app import metrics, platform_settings
from app.errors import AppError
from app.models import Artifact, Environment, EnvironmentBuild, Job, JobAttempt, Node, Quota
from app.schedule import Schedule, load_schedule
from app.schemas import StageIn, UsageIn
from app.storage import storage
from app.telemetry import apply_stages, enter_uploading, record_usage


def _resources(job: Job) -> dict[str, Any]:
    return job.resources if isinstance(job.resources, dict) else {}


def node_fits_job(node: Node, job: Job, env: Environment | None = None) -> bool:
    return fit_failure(node, job, env) is None


def _node_arch(node: Node) -> str:
    return node.compute_capability if node.vram_total_mb > 0 else "cpu"


def fit_failure(node: Node, job: Job, env: Environment | None = None) -> str | None:
    res = _resources(job)
    gpu_class = res.get("gpu_class")
    if settings.strict_node_class:
        if gpu_class != node.compatibility_class:
            return "class"
    elif gpu_class and gpu_class not in {node.compatibility_class, node.gpu_model}:
        return "class"
    min_vram = int(res.get("min_vram_mb") or 0)
    if node.vram_allocatable_mb < min_vram:
        return "vram"
    cpu = int(res.get("cpu_cores") or 0)
    if cpu and node.cpu_cores < cpu:
        return "cpu"
    ram = int(res.get("ram_mb") or 0)
    if ram and node.ram_mb < ram:
        return "ram"
    min_cc = res.get("min_compute_capability")
    if min_cc:
        try:
            if float(node.compute_capability) < float(min_cc):
                return "cc"
        except (TypeError, ValueError):
            return "cc"
    # A GPU job needs an image built and tested for this node's architecture;
    # an empty list means the image is not arch-specific.
    archs = [a for a in (env.gpu_archs if env else None) or [] if a != "cpu"]
    if min_vram and archs and _node_arch(node) not in archs:
        return "env"
    disk_mb = int(res.get("disk_mb") or 0)
    if disk_mb and node.disk_free_bytes - settings.disk_watermark_bytes < disk_mb * 1024 * 1024:
        return "disk"
    return None


def _job_min_vram(job: Job) -> int:
    return int(_resources(job).get("min_vram_mb") or 0)


def is_high_vram_node(node: Node) -> bool:
    return node.vram_allocatable_mb >= settings.high_vram_mb


def job_needs_high_vram(job: Job) -> bool:
    return _job_min_vram(job) >= settings.high_vram_reserve_min


def _aware(ts: datetime, now: datetime) -> datetime:
    if ts.tzinfo is None and now.tzinfo is not None:
        return ts.replace(tzinfo=now.tzinfo)
    return ts


def job_age_seconds(job: Job, now: datetime) -> float:
    created = job.created_at
    if created is None:
        return 0
    return max(0.0, (now - _aware(created, now)).total_seconds())


def runtime_exceeded(attempt: JobAttempt, job: Job, now: datetime) -> bool:
    if attempt.started_at is None:
        return False
    limit = timedelta(seconds=job.max_runtime_seconds + settings.runtime_grace_seconds)
    return now > _aware(attempt.started_at, now) + limit


def reserved_skip(node: Node, job: Job, now: datetime, queued: list[Job]) -> bool:
    if not is_high_vram_node(node) or job_needs_high_vram(job):
        return False
    large_waiting = any(other.id != job.id and job_needs_high_vram(other) and node_fits_job(node, other) for other in queued)
    aged = job_age_seconds(job, now) >= settings.aging_steal_seconds
    if large_waiting:
        return True
    return not aged


def pending_reason_for(node: Node | None, job: Job) -> str:
    if node:
        return f"node {node.hostname} starting"
    res = _resources(job)
    gpu_class = res.get("gpu_class")
    min_vram = int(res.get("min_vram_mb") or 0)
    if gpu_class:
        return f"waiting for node class {gpu_class}"
    if min_vram:
        gb = max(1, round(min_vram / 1024))
        return f"waiting for a {gb} GB GPU"
    return "waiting for a matching node"


async def db_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    assert value is not None
    return value


async def load_job(session: AsyncSession, job_id: uuid.UUID, *, lock: bool = False) -> Job:
    stmt: Select[tuple[Job]] = (
        select(Job).where(Job.id == job_id).options(selectinload(Job.attempts))
    )
    if lock:
        stmt = stmt.with_for_update()
    job = await session.scalar(stmt)
    if job is None:
        raise AppError(404, "not_found", "job not found", job_id=str(job_id))
    return job


async def _active_attempt_for_node(session: AsyncSession, node_id: uuid.UUID) -> JobAttempt | None:
    return await session.scalar(
        select(JobAttempt).where(
            JobAttempt.node_id == node_id,
            JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES),
        )
    )


async def _active_attempt_for_job(session: AsyncSession, job_id: uuid.UUID) -> JobAttempt | None:
    return await session.scalar(
        select(JobAttempt).where(
            JobAttempt.job_id == job_id,
            JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES),
        )
    )


async def _quota_reason(session: AsyncSession, job: Job) -> str | None:
    for subject_type, subject_id in (("user", job.user_id), ("project", job.project_id)):
        quota = await session.scalar(
            select(Quota).where(Quota.subject_type == subject_type, Quota.subject_id == subject_id)
        )
        limit = quota.max_concurrent_jobs if quota else (
            settings.default_user_concurrency if subject_type == "user" else settings.default_project_concurrency
        )
        count = await session.scalar(
            select(func.count())
            .select_from(JobAttempt)
            .join(Job, Job.id == JobAttempt.job_id)
            .where(
                JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES),
                Job.user_id == job.user_id if subject_type == "user" else Job.project_id == job.project_id,
            )
        )
        if int(count or 0) >= limit:
            if subject_type == "user":
                return "waiting for user concurrency slot"
            return "waiting for project concurrency slot"
    return None


def _heartbeat_fresh(node: Node, now: datetime) -> bool:
    if node.last_heartbeat_at is None:
        return False
    limit = timedelta(seconds=settings.heartbeat_interval_seconds * 3)
    hb = node.last_heartbeat_at
    if hb.tzinfo is None and now.tzinfo is not None:
        hb = hb.replace(tzinfo=now.tzinfo)
    return (now - hb) <= limit


def is_online(node: Node, now: datetime) -> bool:
    """A node that could take work if it were free: heartbeating, and neither
    marked offline nor decommissioned. Pending reasons ignore all others, so a
    stale duplicate row of a re-installed PC can not change them."""
    if node.decommissioned_at is not None:
        return False
    return node.eligibility != "offline" and _heartbeat_fresh(node, now)


def is_train_node(node: Node) -> bool:
    role = (getattr(node, "role", None) or "train").strip().lower()
    return role != "build"


def node_can_claim(node: Node, now: datetime, schedule: Schedule | None = None) -> str | None:
    if node.decommissioned_at is not None:
        return "decommissioned"
    if node.eligibility in {"draining", "quarantined"}:
        return node.eligibility
    if node.eligibility == "offline" or not _heartbeat_fresh(node, now):
        return "offline"
    if node.gpu_desktop_busy:
        return "gpu_busy"
    health = node.last_health if isinstance(node.last_health, dict) else {}
    if health.get("agent_healthy") is False:
        return "unhealthy"
    if node.disk_free_bytes < settings.disk_watermark_bytes:
        return "disk"
    if is_outdated(node.agent_version):
        return "outdated"
    if schedule is not None and schedule.blocked(node):
        return "schedule"
    return None


async def _ensure_current_registry(session: AsyncSession, env: Environment) -> bool:
    """Requeue a ready custom build whose image lives on an old registry.

    Returns True when the environment is no longer runnable here.
    """
    from app.envbuild import _rebuild_on_current_registry, on_current_registry

    if not env.name.startswith("build-") or on_current_registry(env.image_digest):
        return False
    build = await session.scalar(
        select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == env.name.removeprefix("build-"))
    )
    if build is None:
        return False
    await _rebuild_on_current_registry(session, env, build)
    return True


async def claim(session: AsyncSession, node: Node) -> JobAttempt | None:
    now = await db_now(session)
    if not is_train_node(node):
        return None
    existing = await _active_attempt_for_node(session, node.id)
    if existing:
        return existing
    await platform_settings.ensure_loaded(session)
    schedule = await load_schedule(session)
    blocked = node_can_claim(node, now, schedule)
    if blocked:
        return None

    # Read candidates without locks and lock only the job this node takes, so
    # concurrent claims from other nodes skip one row, not the whole queue.
    # Skipped jobs keep their stored reason; GET /jobs/{id} recomputes it.
    active = (
        select(JobAttempt.id)
        .where(JobAttempt.job_id == Job.id, JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
        .exists()
    )
    stmt = (
        select(Job)
        .join(Environment, Environment.id == Job.environment_id)
        .where(
            Job.desired_state == "run",
            Job.accepted_attempt_id.is_(None),
            Environment.cuda_preflight_ok.is_(True),
            func.coalesce(Job.pending_reason, "") != "building environment",
            ~active,
        )
        .order_by(Job.priority.desc(), Job.created_at.asc())
        .limit(settings.claim_candidate_limit)
    )
    jobs = (await session.scalars(stmt)).all()
    env_ids = {job.environment_id for job in jobs}
    envs = {
        env.id: env
        for env in (await session.scalars(select(Environment).where(Environment.id.in_(env_ids)))).all()
    } if env_ids else {}
    chosen: Job | None = None
    for job in jobs:
        env = envs.get(job.environment_id)
        if env is not None and await _ensure_current_registry(session, env):
            continue
        if not node_fits_job(node, job, env):
            continue
        if schedule.collision(node, job.max_runtime_seconds):
            continue  # would still be running when the room closes for class
        if await _quota_reason(session, job):
            continue
        if reserved_skip(node, job, now, list(jobs)):
            continue
        locked = await session.scalar(
            select(Job)
            .where(Job.id == job.id, Job.desired_state == "run", Job.accepted_attempt_id.is_(None))
            .with_for_update(skip_locked=True)
        )
        if locked is None or await _active_attempt_for_job(session, job.id):
            continue
        chosen = locked
        break
    if chosen is None:
        return None

    max_n = await session.scalar(
        select(func.coalesce(func.max(JobAttempt.attempt_number), 0)).where(
            JobAttempt.job_id == chosen.id
        )
    )
    attempt_id = uuid.uuid4()
    attempt = JobAttempt(
        id=attempt_id,
        job_id=chosen.id,
        attempt_number=int(max_n or 0) + 1,
        node_id=node.id,
        fencing_token=uuid.uuid4(),
        lease_expires_at=now + timedelta(seconds=settings.lease_ttl_seconds),
        observed_state="leased",
        output_namespace=f"jobs/{chosen.id}/attempts/{attempt_id}/",
    )
    chosen.pending_reason = pending_reason_for(node, chosen)
    try:
        async with session.begin_nested():
            session.add(attempt)
            await session.flush()
        metrics.attempt_created(session, chosen.created_at, attempt.attempt_number, now)
        return attempt
    except IntegrityError:
        existing = await _active_attempt_for_node(session, node.id)
        if existing:
            return existing
        raise


async def explain_pending(session: AsyncSession, job: Job) -> str | None:
    from app.status import derived_status

    status = derived_status(job)
    if status != "queued":
        return job.pending_reason
    await platform_settings.ensure_loaded(session)
    now = await db_now(session)
    nodes = [n for n in (await session.scalars(select(Node))).all() if is_online(n, now)]
    gpu_class = _resources(job).get("gpu_class")
    class_nodes = [
        n
        for n in nodes
        if is_train_node(n)
        and n.eligibility != "quarantined"
        and (not gpu_class or n.compatibility_class == gpu_class or (not settings.strict_node_class and n.gpu_model == gpu_class))
    ]
    if gpu_class and not class_nodes:
        return f"waiting for node class {gpu_class}"
    vram_ok = [n for n in class_nodes if fit_failure(n, job) != "vram"]
    if _job_min_vram(job) and not vram_ok:
        gb = max(1, round(_job_min_vram(job) / 1024))
        return f"waiting for a {gb} GB GPU"
    env = await session.get(Environment, job.environment_id)
    arch_ok = [n for n in vram_ok if fit_failure(n, job, env) != "env"]
    if vram_ok and not arch_ok:
        return "waiting for a GPU this environment supports"
    if arch_ok and all(fit_failure(n, job, env) == "disk" for n in arch_ok):
        return "waiting for disk on eligible nodes"
    fitting = [n for n in class_nodes if node_fits_job(n, job, env)]
    if fitting and all(n.disk_free_bytes < settings.disk_watermark_bytes for n in fitting):
        return "waiting for disk on eligible nodes"
    quota_reason = await _quota_reason(session, job)
    if quota_reason:
        return quota_reason
    usable = [n for n in fitting if node_can_claim(n, now) is None]
    schedule = await load_schedule(session)
    closed = schedule.reserved_reason(usable)
    if closed:
        return closed
    opens = [start for n in usable if (start := schedule.collision(n, job.max_runtime_seconds)) is not None]
    if usable and len(opens) == len(usable):
        return f"lab reserved for teaching from {min(opens):%H:%M}; lower the max runtime to start sooner"
    claimable = usable
    queued = (
        await session.scalars(
            select(Job).where(Job.desired_state == "run", Job.accepted_attempt_id.is_(None))
        )
    ).all()
    if claimable and all(reserved_skip(n, job, now, list(queued)) for n in claimable):
        return "waiting for a smaller GPU or high-VRAM reservation"
    if job.pending_reason:
        return job.pending_reason
    return "waiting for a matching node"


def _require_token(attempt: JobAttempt, token: uuid.UUID) -> None:
    if attempt.fencing_token != token:
        raise AppError(
            409,
            "fenced",
            "stale fencing token",
            job_id=str(attempt.job_id),
            attempt_id=str(attempt.id),
        )


async def _get_attempt(session: AsyncSession, attempt_id: uuid.UUID) -> JobAttempt:
    attempt = await session.scalar(
        select(JobAttempt).where(JobAttempt.id == attempt_id).with_for_update()
    )
    if attempt is None:
        raise AppError(404, "not_found", "attempt not found", attempt_id=str(attempt_id))
    return attempt


async def renew(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    observed_state: str | None,
    resource_usage: dict | None,
    usage: UsageIn | None = None,
    stages: list[StageIn] | None = None,
) -> tuple[JobAttempt, Job]:
    attempt = await _get_attempt(session, attempt_id)
    _require_token(attempt, token)
    job = await load_job(session, attempt.job_id, lock=True)
    now = await db_now(session)

    if attempt.observed_state in TERMINAL_ATTEMPT_STATES:
        if job.accepted_attempt_id and job.accepted_attempt_id != attempt.id:
            raise AppError(409, "fenced", "attempt superseded", job_id=str(job.id), attempt_id=str(attempt.id))
        return attempt, job

    if observed_state and observed_state != attempt.observed_state:
        _apply_transition(attempt, observed_state, now)

    if job.desired_state == "cancel" and attempt.observed_state in ACTIVE_ATTEMPT_STATES:
        if attempt.observed_state != "cancelling":
            attempt.observed_state = "cancelling"

    if attempt.observed_state == "unknown" and observed_state in {"running", "starting", "uploading", "leased"}:
        attempt.observed_state = observed_state

    # Backstop for an agent that keeps renewing but never enforces the limit.
    if attempt.observed_state in ACTIVE_ATTEMPT_STATES and runtime_exceeded(attempt, job, now):
        attempt.failure_category = "timeout"
        attempt.observed_state = "cancelling"

    attempt.lease_expires_at = now + timedelta(seconds=settings.lease_ttl_seconds)
    if resource_usage is not None:
        attempt.resource_usage = resource_usage
    if stages:
        apply_stages(attempt, stages, now, session)
    if usage is not None:
        await record_usage(session, attempt, usage, now)
    return attempt, job


def _apply_transition(attempt: JobAttempt, new_state: str, now: datetime) -> None:
    current = attempt.observed_state
    if current == new_state:
        return
    allowed = FORWARD_TRANSITIONS.get(current, set())
    if new_state not in allowed:
        raise AppError(
            409,
            "illegal_transition",
            f"cannot move from {current} to {new_state}",
            attempt_id=str(attempt.id),
        )
    attempt.observed_state = new_state
    if new_state in {"starting", "running"} and attempt.started_at is None:
        attempt.started_at = now
    if new_state == "uploading":
        enter_uploading(attempt, now)
    if new_state in TERMINAL_ATTEMPT_STATES:
        attempt.completed_at = now


async def on_event(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    state: str,
) -> JobAttempt:
    attempt = await _get_attempt(session, attempt_id)
    _require_token(attempt, token)
    job = await load_job(session, attempt.job_id, lock=True)
    if job.accepted_attempt_id and job.accepted_attempt_id != attempt.id:
        raise AppError(409, "fenced", "attempt superseded", job_id=str(job.id), attempt_id=str(attempt.id))
    now = await db_now(session)
    if job.desired_state == "cancel" and state not in {"cancelling", "cancelled"}:
        attempt.observed_state = "cancelling"
        return attempt
    _apply_transition(attempt, state, now)
    return attempt


async def request_cancel(session: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await load_job(session, job_id, lock=True)
    if job.accepted_attempt_id:
        accepted = next((a for a in job.attempts if a.id == job.accepted_attempt_id), None)
        if accepted and accepted.observed_state == "completed":
            raise AppError(409, "already_completed", "job already completed", job_id=str(job.id))
    job.desired_state = "cancel"
    attempt = await _active_attempt_for_job(session, job.id)
    now = await db_now(session)
    if attempt is None:
        if job.completed_at is None:
            metrics.job_finished(session, "cancelled")
        job.completed_at = now
        job.pending_reason = None
        return job
    if attempt.observed_state not in TERMINAL_ATTEMPT_STATES:
        attempt.observed_state = "cancelling"
    return job


async def _promote(session: AsyncSession, job: Job, attempt: JobAttempt, manifest: list[dict]) -> None:
    for item in manifest:
        relative = str(item["relative_path"]).lstrip("/")
        src = f"{attempt.output_namespace}outputs/{relative}"
        dst = f"jobs/{job.id}/promoted/{relative}"
        if not storage.exists(src):
            raise AppError(409, "missing_output", f"output object missing: {relative}", job_id=str(job.id))
        expected = str(item.get("checksum_sha256") or "")
        data = storage.get_bytes(src)
        actual = __import__("hashlib").sha256(data).hexdigest()
        if expected and actual != expected:
            raise AppError(409, "checksum_mismatch", f"checksum mismatch for {relative}", job_id=str(job.id))
        storage.copy(src, dst)
        session.add(
            Artifact(
                job_id=job.id,
                attempt_id=attempt.id,
                type="output",
                object_key=dst,
                size_bytes=int(item.get("size_bytes") or len(data)),
                checksum_sha256=actual,
            )
        )


async def complete(
    session: AsyncSession,
    attempt_id: uuid.UUID,
    token: uuid.UUID,
    exit_code: int,
    failure_category: str | None,
    output_manifest: list[dict] | None,
) -> dict[str, Any]:
    attempt = await _get_attempt(session, attempt_id)
    _require_token(attempt, token)
    job = await load_job(session, attempt.job_id, lock=True)
    now = await db_now(session)
    manifest = output_manifest or []

    if attempt.observed_state in TERMINAL_ATTEMPT_STATES:
        payload = attempt.completion_payload or {}
        if payload.get("exit_code") != exit_code:
            raise AppError(
                409,
                "conflict",
                "complete already accepted with a different exit code",
                job_id=str(job.id),
                attempt_id=str(attempt.id),
            )
        return payload

    if job.accepted_attempt_id and job.accepted_attempt_id != attempt.id:
        raise AppError(409, "fenced", "attempt superseded", job_id=str(job.id), attempt_id=str(attempt.id))

    if job.desired_state == "cancel" and job.accepted_attempt_id is None:
        attempt.observed_state = "cancelled"
        attempt.failure_category = "cancelled"
        attempt.exit_code = exit_code
        attempt.completed_at = now
        job.accepted_attempt_id = attempt.id
        job.completed_at = now
        job.pending_reason = None
        metrics.attempt_ended(session, attempt.started_at, now)
        metrics.job_finished(session, "cancelled")
        payload = {
            "accepted": False,
            "status": "cancelled",
            "error": "cancelled",
            "exit_code": exit_code,
            "attempt_id": str(attempt.id),
            "job_id": str(job.id),
        }
        attempt.completion_payload = payload
        return payload

    if exit_code == 0 and not failure_category:
        total = sum(int(item.get("size_bytes") or 0) for item in manifest)
        too_big = any(int(item.get("size_bytes") or 0) > settings.output_file_bytes for item in manifest)
        if total > settings.output_bytes_per_attempt or too_big:
            failure_category = "resource"
            exit_code = 1

    if exit_code == 0 and not failure_category:
        await _promote(session, job, attempt, manifest)
        attempt.observed_state = "completed"
        attempt.exit_code = 0
        attempt.completed_at = now
        job.accepted_attempt_id = attempt.id
        job.completed_at = now
        job.pending_reason = None
        status = "completed"
        metrics.job_finished(session, "completed")
    else:
        if attempt.failure_category == "timeout":
            category = "timeout"
        else:
            category = failure_category or "user_code"
        attempt.observed_state = "failed" if category != "cancelled" else "cancelled"
        attempt.failure_category = category
        attempt.exit_code = exit_code
        attempt.completed_at = now
        retry = (
            category in RETRYABLE_FAILURES
            and job.desired_state == "run"
            and attempt.attempt_number < job.max_attempts
        )
        if retry:
            job.accepted_attempt_id = None
            job.pending_reason = "retrying after node lost"
            job.completed_at = None
            status = "queued"
        else:
            job.accepted_attempt_id = attempt.id
            job.completed_at = now
            job.pending_reason = None
            status = "failed" if attempt.observed_state == "failed" else "cancelled"
            metrics.job_finished(session, status)

    metrics.attempt_ended(session, attempt.started_at, now)
    payload = {
        "accepted": True,
        "status": status,
        "exit_code": exit_code,
        "attempt_id": str(attempt.id),
        "job_id": str(job.id),
    }
    attempt.completion_payload = payload
    return payload


async def expire_leases(session: AsyncSession) -> None:
    now = await db_now(session)
    attempts = (
        await session.scalars(
            select(JobAttempt)
            .where(JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
            .with_for_update()
        )
    ).all()
    grace = timedelta(seconds=settings.disconnect_grace_seconds)
    for attempt in attempts:
        job = await load_job(session, attempt.job_id, lock=True)
        if attempt.observed_state != "unknown" and now > attempt.lease_expires_at:
            attempt.observed_state = "unknown"
            continue
        if attempt.observed_state == "unknown" and now > attempt.lease_expires_at + grace:
            if job.desired_state == "cancel":
                attempt.observed_state = "cancelled"
                attempt.failure_category = "cancelled"
                attempt.completed_at = now
                job.accepted_attempt_id = attempt.id
                job.completed_at = now
                job.pending_reason = None
                metrics.job_finished(session, "cancelled")
                continue
            metrics.lease_lost(session)
            await _fail_attempt(session, job, attempt, "lease_lost", now)


async def _fail_attempt(
    session: AsyncSession,
    job: Job,
    attempt: JobAttempt,
    category: str,
    now: datetime,
) -> None:
    if attempt.observed_state in TERMINAL_ATTEMPT_STATES:
        return
    attempt.observed_state = "failed"
    attempt.failure_category = category
    attempt.completed_at = now
    retry = (
        category in RETRYABLE_FAILURES
        and job.desired_state == "run"
        and attempt.attempt_number < job.max_attempts
    )
    if retry:
        job.accepted_attempt_id = None
        job.pending_reason = "retrying after node lost"
        job.completed_at = None
    else:
        job.accepted_attempt_id = attempt.id
        job.completed_at = now
        job.pending_reason = None
        metrics.job_finished(session, "failed")


async def on_node_boot(session: AsyncSession, node_id: uuid.UUID) -> None:
    now = await db_now(session)
    attempts = (
        await session.scalars(
            select(JobAttempt).where(
                JobAttempt.node_id == node_id,
                JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES),
            )
        )
    ).all()
    for attempt in attempts:
        job = await load_job(session, attempt.job_id, lock=True)
        await _fail_attempt(session, job, attempt, "node_lost", now)


async def mark_offline_nodes(session: AsyncSession) -> None:
    """Flip nodes between offline and eligible by heartbeat age. Both are single
    UPDATEs that test the heartbeat in their WHERE clause: a heartbeat landing
    while this runs must not be overwritten by a decision made on its old value."""
    now = await db_now(session)
    cutoff = now - timedelta(seconds=settings.heartbeat_interval_seconds * 3)
    fleet = (Node.decommissioned_at.is_(None), Node.eligibility.not_in(("draining", "quarantined")))
    await session.execute(
        update(Node)
        .where(*fleet, Node.eligibility != "offline", or_(Node.last_heartbeat_at.is_(None), Node.last_heartbeat_at < cutoff))
        .values(eligibility="offline")
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(Node)
        .where(*fleet, Node.eligibility == "offline", Node.last_heartbeat_at >= cutoff)
        .values(eligibility="eligible")
        .execution_options(synchronize_session=False)
    )


async def claim_payload(session: AsyncSession, attempt: JobAttempt) -> dict[str, Any]:
    job = await load_job(session, attempt.job_id)
    env = await session.get(Environment, job.environment_id)
    return {
        "attempt_id": str(attempt.id),
        "job_id": str(job.id),
        "fencing_token": str(attempt.fencing_token),
        "lease_expires_at": attempt.lease_expires_at.isoformat(),
        "image_digest": env.image_digest if env else "sha256:dev",
        "entrypoint": job.entrypoint,
        "args": job.args,
        "resources": job.resources,
        "input": {
            "artifact_id": str(job.submission_artifact_id) if job.submission_artifact_id else None,
            "url": f"/attempts/{attempt.id}/input",
        },
        "output_namespace": attempt.output_namespace,
        "log_upload": {"url": f"/attempts/{attempt.id}/logs"},
        "max_runtime_seconds": job.max_runtime_seconds,
    }
