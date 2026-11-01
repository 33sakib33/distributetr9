from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import agent_logs, platform_settings
from app.audit import record_audit
from app.availability import queue_info
from app.archive import ArchiveError, candidate_entrypoints, entrypoint_in_archive, prepare_upload, suggest_entrypoint
from app.config import settings
from app.db import SessionLocal, get_session
from app.deps import get_current_user, get_job_for_user, require_admin, require_project_member
from app.envbuild import resolve_environment, restart_failed_build
from app.enums import ACTIVE_ATTEMPT_STATES
from app.errors import AppError
from app.identity import INVITE_PREFIX, allowed_domains, email_allowed, is_invited
from app.failure_explain import explain_failure, log_tail
from app.jobspec import job_resources, waiting_reason
from app.models import (
    EnvironmentBuild,
    Artifact,
    Environment,
    Job,
    JobAttempt,
    Node,
    NodeCredential,
    NodeEvent,
    NodeHealth,
    Project,
    ProjectMember,
    User,
)
from app.ratelimit import limit_user
from app.scheduler import request_cancel
from app.agent_bundle import target_version
from app.schemas import MemberAdd, MetaIn, ProjectCreate, QuarantineIn, RingIn
from app.seed import ensure_project_quota, ensure_user_quota
from app.serialize import load_job, node_for_job, serialize_job
from app.status import derived_status, latest_attempt
from app.storage import sha256_bytes, storage
from app.telemetry import building_environment, build_for_job, stages_for, usage_for_attempt

router = APIRouter()
_TERMINAL_JOB = {"completed", "failed", "cancelled"}


def _log_chunks_after(attempt, after_seq: int) -> list[dict]:
    if attempt is None:
        return []
    chunks = []
    for seq in range(after_seq + 1, int(attempt.last_log_seq) + 1):
        key = f"{attempt.output_namespace}logs/{seq}.jsonl.gz"
        try:
            raw = storage.get_log_chunk(key)
        except Exception:
            continue
        for line in raw.decode().splitlines():
            if line.strip():
                chunks.append(json.loads(line))
    return chunks


@router.get("/health")
async def health() -> dict:
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_current_user)) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
    }


@router.post("/projects", status_code=201)
async def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    exists = await session.scalar(
        select(Project).where(Project.created_by == user.id, Project.name == body.name)
    )
    if exists:
        raise AppError(409, "conflict", "project name already exists")
    project = Project(name=body.name, created_by=user.id)
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=user.id, role="owner"))
    await ensure_project_quota(session, project.id)
    await session.commit()
    return {"id": str(project.id), "name": project.name}


@router.get("/projects")
async def list_projects(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.scalars(
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user.id)
        )
    ).all()
    return {"projects": [{"id": str(p.id), "name": p.name} for p in rows]}


@router.post("/projects/{project_id}/members", status_code=201)
async def add_member(
    project_id: uuid.UUID,
    body: MemberAdd,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    member = await require_project_member(project_id, user, session)
    if member.role != "owner" and not user.is_admin:
        raise AppError(403, "forbidden", "only owners can add members")
    email = body.email.strip().lower()
    domains = allowed_domains()
    if "@" not in email or not email_allowed(email):
        raise AppError(
            422, "email_domain",
            f"only {', '.join('@' + d for d in domains)} addresses can be added" if domains else "enter a valid email address",
        )
    other = await session.scalar(select(User).where(User.email == email))
    if other is None:
        other = User(
            university_sub=f"{INVITE_PREFIX}{email}",
            email=email,
            display_name=email.split("@")[0],
            is_admin=False,
        )
        session.add(other)
        await session.flush()
        await ensure_user_quota(session, other)
    existing = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id, ProjectMember.user_id == other.id
        )
    )
    if existing:
        return {"user_id": str(other.id), "role": existing.role, "invited": is_invited(other)}
    session.add(ProjectMember(project_id=project_id, user_id=other.id, role=body.role))
    record_audit(
        session, user, "member.add", "project", project_id,
        {"user_id": str(other.id), "email": other.email, "role": body.role},
    )
    await session.commit()
    return {"user_id": str(other.id), "role": body.role, "invited": is_invited(other)}


@router.get("/environments")
async def list_environments(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.scalars(
            select(Environment)
            .where(~Environment.name.like("build-%"), Environment.retired_at.is_(None))
            .order_by(Environment.name)
        )
    ).all()
    return {
        "environments": [
            {
                "id": str(e.id),
                "name": e.name,
                "framework": e.framework,
                "python_version": e.python_version,
                "cuda_version": e.cuda_version,
                "image_digest": e.image_digest,
                "cuda_preflight_ok": e.cuda_preflight_ok,
            }
            for e in rows
        ]
    }


@router.post(
    "/jobs",
    status_code=201,
    dependencies=[Depends(limit_user("jobs", "rate_limit_jobs_per_minute"))],
)
async def create_job(
    project_id: uuid.UUID = Form(),
    entrypoint: str = Form(default=""),
    environment_id: uuid.UUID | None = Form(default=None),
    requirements: str = Form(default=""),
    args: str = Form(default="[]"),
    resources: str = Form(default="{}"),
    archive: UploadFile = File(),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await require_project_member(project_id, user, session)
    idem_key = (idempotency_key or "").strip()[:200] or None
    if idem_key:
        existing = await session.scalar(
            select(Job.id).where(Job.user_id == user.id, Job.idempotency_key == idem_key)
        )
        if existing is not None:
            job = await load_job(session, existing)
            return serialize_job(job, await node_for_job(session, job))
    entrypoint = entrypoint.strip()
    if entrypoint and not entrypoint.endswith(".py"):
        raise AppError(400, "invalid", "entrypoint must be a .py file")
    try:
        arg_list = json.loads(args)
        res = json.loads(resources)
        if not isinstance(arg_list, list) or not isinstance(res, dict):
            raise ValueError("bad json types")
    except ValueError as exc:
        raise AppError(400, "invalid", "args and resources must be JSON") from exc

    req_text = (requirements or "").strip()
    if req_text and not settings.registry_url.strip():
        raise AppError(
            400,
            "invalid",
            "custom requirements.txt needs REGISTRY_URL; use a catalog environment until the registry is up",
        )
    catalog = await session.get(Environment, environment_id) if environment_id else None
    if catalog is not None and catalog.retired_at is not None:
        raise AppError(400, "invalid", f"environment {catalog.name} is retired; choose another")
    if req_text:
        try:
            env, build = await resolve_environment(
                session,
                requirements_text=req_text,
                gpu_class=res.get("gpu_class"),
                base_env=catalog if catalog and not str(catalog.name).startswith("build-") else None,
            )
        except ValueError as exc:
            raise AppError(400, "invalid", str(exc)) from exc
    else:
        env = catalog
        build = None
        if env is None or not env.cuda_preflight_ok:
            raise AppError(400, "invalid", "unknown or untested environment")

    data = await archive.read()
    if not data:
        raise AppError(400, "invalid", "archive is empty")
    if len(data) > settings.max_upload_bytes:
        raise AppError(400, "invalid", "archive exceeds size limit")
    try:
        upload = prepare_upload(data, archive.filename or "input.tar.gz")
        # A lone script is its own entrypoint; otherwise the likeliest script when none was named.
        wanted = entrypoint or upload.script or suggest_entrypoint(candidate_entrypoints(upload.names))
        if not wanted:
            raise ArchiveError("no .py file found in the archive; name the entrypoint")
        resolved = entrypoint_in_archive(upload.names, wanted)
    except ArchiveError as exc:
        raise AppError(400, "invalid", str(exc)) from exc
    data = upload.data

    resources, runtime = job_resources(res, user)

    job = Job(
        user_id=user.id,
        project_id=project_id,
        entrypoint=resolved,
        args=[str(a) for a in arg_list],
        environment_id=env.id,
        resources=resources,
        max_runtime_seconds=runtime,
        pending_reason=(
            "building environment"
            if build is not None and build.state != "ready"
            else waiting_reason(resources["min_vram_mb"])
        ),
        idempotency_key=idem_key,
    )
    session.add(job)
    try:
        await session.flush()
    except IntegrityError:
        # A concurrent submit with the same Idempotency-Key won the insert.
        await session.rollback()
        existing = await session.scalar(
            select(Job.id).where(Job.user_id == user.id, Job.idempotency_key == idem_key)
        )
        if existing is None:
            raise
        job = await load_job(session, existing)
        return serialize_job(job, await node_for_job(session, job))
    key = f"submissions/{job.id}/input.{upload.extension}"
    checksum = sha256_bytes(data)
    try:
        storage.put_bytes(key, data, "application/zip" if upload.extension == "zip" else "application/gzip")
    except Exception as exc:
        await session.rollback()
        raise AppError(503, "storage", "failed to store submission") from exc
    artifact = Artifact(
        job_id=job.id,
        type="input",
        object_key=key,
        size_bytes=len(data),
        checksum_sha256=checksum,
    )
    session.add(artifact)
    await session.flush()
    job.submission_artifact_id = artifact.id
    await session.commit()
    job = await load_job(session, job.id)
    return serialize_job(job, await node_for_job(session, job))


@router.get("/jobs")
async def list_jobs(
    project_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(Job).options(selectinload(Job.attempts)).order_by(Job.created_at.desc())
    if project_id:
        await require_project_member(project_id, user, session)
        stmt = stmt.where(Job.project_id == project_id)
    else:
        stmt = stmt.join(ProjectMember, ProjectMember.project_id == Job.project_id).where(
            ProjectMember.user_id == user.id
        )
    rows = (await session.scalars(stmt)).unique().all()
    return {"jobs": [serialize_job(j, await node_for_job(session, j)) for j in rows]}


@router.get("/jobs/{job_id}")
async def get_job(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.scheduler import explain_pending

    job.pending_reason = await explain_pending(session, job)
    await session.commit()
    payload = serialize_job(job, await node_for_job(session, job))
    owner = await session.get(User, job.user_id)
    project = await session.get(Project, job.project_id)
    payload["user_email"] = owner.email if owner else None
    payload["project_name"] = project.name if project else None
    build = await build_for_job(session, job)
    payload["stages"] = stages_for(latest_attempt(job), build)
    payload["building_environment"] = building_environment(build)
    status = payload["status"]
    payload["queue_position"], payload["est_start_seconds"] = (
        await queue_info(session, job) if status == "queued" else (None, None)
    )
    payload["failure_explanation"] = None
    if status == "failed":
        attempt = latest_attempt(job)
        payload["failure_explanation"] = explain_failure(
            attempt, await log_tail(attempt), build_failed=attempt is None
        )
    return payload


@router.get("/jobs/{job_id}/usage")
async def get_job_usage(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt = latest_attempt(job)
    if attempt is None:
        return {
            "attempt_id": None,
            "samples": [],
            "peak_vram_mb": None,
            "peak_ram_mb": None,
            "avg_gpu_pct": None,
            "vram_total_mb": None,
        }
    return await usage_for_attempt(session, attempt, await session.get(Node, attempt.node_id))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await load_job(session, job_id)
    await require_project_member(job.project_id, user, session)
    owner_id = job.user_id
    job = await request_cancel(session, job_id)
    if user.is_admin and owner_id != user.id:
        record_audit(session, user, "job.cancel", "job", job_id, {"owner_id": str(owner_id)})
    await session.commit()
    job = await load_job(session, job_id)
    return serialize_job(job, await node_for_job(session, job))


@router.post("/jobs/{job_id}/retry")
async def retry_job(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    status = derived_status(job)
    if status != "failed":
        raise AppError(409, "conflict", "only failed jobs can be retried")
    if job.desired_state == "cancel":
        raise AppError(409, "conflict", "job is cancelled")
    latest = latest_attempt(job)
    if latest and latest.attempt_number >= job.max_attempts:
        raise AppError(409, "conflict", "retries exhausted")
    job.desired_state = "run"
    job.accepted_attempt_id = None
    job.completed_at = None
    job.notified_at, job.notify_tries = None, 0  # tell the owner again when it finishes
    job.pending_reason = (
        "building environment" if await restart_failed_build(session, job) else "waiting for a matching node"
    )
    await session.commit()
    job = await load_job(session, job.id)
    return serialize_job(job, await node_for_job(session, job))


@router.get("/jobs/{job_id}/logs")
async def get_logs(
    job: Job = Depends(get_job_for_user),
    after_seq: int = 0,
) -> dict:
    attempt = latest_attempt(job)
    if attempt is None:
        return {"chunks": [], "last_seq": 0}
    chunks = _log_chunks_after(attempt, after_seq)
    return {"chunks": chunks, "last_seq": attempt.last_log_seq}


@router.get("/jobs/{job_id}/build-logs")
async def get_build_logs(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    env = await session.get(Environment, job.environment_id)
    if env is None or not str(env.name).startswith("build-"):
        return {"state": None, "error": None, "text": ""}
    env_hash = env.name.removeprefix("build-")
    build = await session.scalar(
        select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == env_hash)
    )
    if build is None:
        return {"state": None, "error": None, "text": ""}
    text = ""
    key = f"builds/{build.id}/log.txt"
    if storage.exists(key):
        text = storage.get_bytes(key).decode(errors="replace")
    return {"state": build.state, "error": build.error_message, "text": text}


@router.get("/jobs/{job_id}/logs/stream")
async def stream_logs(
    request: Request,
    after_seq: int = 0,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    job: Job = Depends(get_job_for_user),
):
    cursor = after_seq
    if last_event_id and after_seq == 0:
        try:
            cursor = int(last_event_id)
        except ValueError:
            cursor = 0

    async def events():
        nonlocal cursor
        idle_loops = 0
        while True:
            if await request.is_disconnected():
                return
            async with SessionLocal() as session:
                fresh = await load_job(session, job.id)
                attempt = latest_attempt(fresh)
                status = derived_status(fresh)
                chunks = _log_chunks_after(attempt, cursor)
            for chunk in chunks:
                seq = int(chunk.get("seq") or 0)
                payload = json.dumps(
                    {
                        "seq": seq,
                        "stream": chunk.get("stream", "stdout"),
                        "line": chunk.get("line", ""),
                    }
                )
                yield f"id: {seq}\nevent: log\ndata: {payload}\n\n"
                cursor = max(cursor, seq)
            if status in _TERMINAL_JOB:
                yield f"event: done\ndata: {json.dumps({'status': status, 'last_seq': cursor})}\n\n"
                return
            idle_loops += 1
            if idle_loops % 25 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}/artifacts")
async def get_artifacts(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        await session.scalars(select(Artifact).where(Artifact.job_id == job.id).order_by(Artifact.created_at))
    ).all()
    return {
        "artifacts": [
            {
                "id": str(a.id),
                "type": a.type,
                "object_key": a.object_key,
                "size_bytes": a.size_bytes,
                "checksum_sha256": a.checksum_sha256,
                "download": f"/artifacts/{a.id}/download",
            }
            for a in rows
            if a.type in {"output", "log", "input"}
        ]
    }


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    from fastapi.responses import Response

    artifact = await session.get(Artifact, artifact_id)
    if artifact is None:
        raise AppError(404, "not_found", "artifact not found")
    if artifact.job_id:
        job = await load_job(session, artifact.job_id)
        await require_project_member(job.project_id, user, session)
    data = storage.get_bytes(artifact.object_key)
    filename = artifact.object_key.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _serialize_node(n: Node) -> dict:
    return {
        "id": str(n.id),
        "hostname": n.hostname,
        "gpu_model": n.gpu_model,
        "compatibility_class": n.compatibility_class,
        "vram_allocatable_mb": n.vram_allocatable_mb,
        "eligibility": n.eligibility,
        "eligibility_reason": n.eligibility_reason,
        "role": n.role,
        "last_heartbeat_at": n.last_heartbeat_at.isoformat() if n.last_heartbeat_at else None,
        "gpu_desktop_busy": n.gpu_desktop_busy,
        "disk_free_bytes": n.disk_free_bytes,
        "agent_version": n.agent_version,
        "update_ring": n.update_ring,
        "last_restart": n.last_restart,
        "cpu_cores": n.cpu_cores,
        "ram_mb": n.ram_mb,
        "room": n.room,
        "room_source": n.room_source,
        "labels": n.labels or [],
        "decommissioned_at": n.decommissioned_at.isoformat() if n.decommissioned_at else None,
    }


# A health sample older than this is not shown as the PC's current load.
LIVE_SAMPLE_SECONDS = 300


async def _live_state(session: AsyncSession, node_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """Current load and job of each PC in two queries, however many PCs there are:
    the newest health sample per node and the active attempt per node."""
    from app.scheduler import db_now

    live = {
        node_id: {"gpu_pct": None, "gpu_temp_c": None, "busy": False, "current_attempt": None}
        for node_id in node_ids
    }
    if not node_ids:
        return live
    fresh = (await db_now(session)) - timedelta(seconds=LIVE_SAMPLE_SECONDS)
    samples = (
        await session.scalars(
            select(NodeHealth)
            .where(NodeHealth.node_id.in_(node_ids), NodeHealth.at >= fresh)
            .distinct(NodeHealth.node_id)
            .order_by(NodeHealth.node_id, NodeHealth.at.desc())
        )
    ).all()
    for sample in samples:
        live[sample.node_id].update(gpu_pct=sample.gpu_pct, gpu_temp_c=sample.gpu_temp_c)
    attempts = (
        await session.scalars(
            select(JobAttempt).where(
                JobAttempt.node_id.in_(node_ids), JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES)
            )
        )
    ).all()
    for attempt in attempts:
        live[attempt.node_id].update(
            busy=True,
            current_attempt={
                "attempt_id": str(attempt.id),
                "job_id": str(attempt.job_id),
                "state": attempt.observed_state,
            },
        )
    return live


@router.get("/nodes")
async def list_nodes(
    room: str | None = None,
    include_decommissioned: bool = False,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The fleet. Forgotten PCs are left out unless `include_decommissioned=1`;
    `room` (any case) keeps one room; `rooms` lists every room of the fleet."""
    stmt = select(Node).order_by(Node.hostname)
    if not include_decommissioned:
        stmt = stmt.where(Node.decommissioned_at.is_(None))
    rooms = (
        await session.scalars(
            select(Node.room)
            .where(Node.room.is_not(None), Node.decommissioned_at.is_(None))
            .distinct()
            .order_by(Node.room)
        )
    ).all()
    if room and room.strip():
        stmt = stmt.where(func.lower(Node.room) == room.strip().lower())
    rows = (await session.scalars(stmt)).all()
    await platform_settings.ensure_loaded(session)
    live = await _live_state(session, [n.id for n in rows])
    return {
        "nodes": [{**_serialize_node(n), **live[n.id]} for n in rows],
        "rooms": list(rooms),
        "agent_target_version": target_version(),
        "agent_rollout_ring": platform_settings.rollout_ring(),
    }


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.scheduler import db_now

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    since = (await db_now(session)) - timedelta(hours=24)
    history = (
        await session.scalars(
            select(NodeHealth)
            .where(NodeHealth.node_id == node.id, NodeHealth.at >= since)
            .order_by(NodeHealth.at)
        )
    ).all()
    attempts = (
        await session.scalars(
            select(JobAttempt)
            .where(JobAttempt.node_id == node.id)
            .order_by(JobAttempt.created_at.desc())
            .limit(20)
        )
    ).all()
    events = (
        await session.scalars(
            select(NodeEvent).where(NodeEvent.node_id == node.id).order_by(NodeEvent.at.desc()).limit(20)
        )
    ).all()
    return {
        **_serialize_node(node),
        "vram_total_mb": node.vram_total_mb,
        "compute_capability": node.compute_capability,
        "driver_version": node.driver_version,
        "last_health": node.last_health,
        "health_history": [
            {
                "at": h.at.isoformat(),
                "gpu_temp_c": h.gpu_temp_c,
                "gpu_pct": h.gpu_pct,
                "disk_free_bytes": h.disk_free_bytes,
                "busy": h.busy,
            }
            for h in history
        ],
        "recent_attempts": [
            {
                "attempt_id": str(a.id),
                "job_id": str(a.job_id),
                "attempt_number": a.attempt_number,
                "state": a.observed_state,
                "exit_code": a.exit_code,
                "failure_category": a.failure_category,
                "created_at": a.created_at.isoformat() if a.created_at else None,
                "started_at": a.started_at.isoformat() if a.started_at else None,
                "completed_at": a.completed_at.isoformat() if a.completed_at else None,
            }
            for a in attempts
        ],
        "events": [{"at": e.at.isoformat(), "kind": e.kind, "payload": e.payload} for e in events],
    }


@router.post("/nodes/{node_id}/logs/request")
async def request_node_logs(
    node_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ask the agent for the tail of its log; it uploads on its next heartbeat."""
    from app.scheduler import db_now

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    agent_logs.request_logs(node, await db_now(session))
    record_audit(session, admin, "node.logs_request", "node", node.id, {"hostname": node.hostname})
    await session.commit()
    return agent_logs.read_logs(node) | {"text": None}


@router.get("/nodes/{node_id}/logs")
async def get_node_logs(
    node_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    return agent_logs.read_logs(node)


@router.post("/nodes/{node_id}/revoke")
async def revoke_node(
    node_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Invalidate a node's token (lost, stolen or re-imaged PC). The PC must be
    reinstalled with setup-lab-node.sh --reset-identity to join again."""
    from app.scheduler import db_now, on_node_boot

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    cred = await session.get(NodeCredential, node_id)
    if cred is not None and cred.revoked_at is None:
        cred.revoked_at = await db_now(session)
    node.eligibility = "quarantined"
    node.eligibility_reason = "credential revoked"
    await on_node_boot(session, node.id)
    record_audit(session, admin, "node.revoke", "node", node.id, {"hostname": node.hostname})
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility, "reason": node.eligibility_reason}


@router.post("/nodes/{node_id}/forget")
async def forget_node(
    node_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Decommission a PC that is gone for good: revoke its credential, fail its
    active attempts (their jobs retry elsewhere) and take it out of the fleet,
    the scheduler, alerts and metrics. Its history stays."""
    from app.alerts import resolve_node_alerts
    from app.scheduler import db_now, on_node_boot

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    if node.decommissioned_at is None:
        now = await db_now(session)
        cred = await session.get(NodeCredential, node_id)
        if cred is not None and cred.revoked_at is None:
            cred.revoked_at = now
        node.eligibility = "quarantined"
        node.eligibility_reason = "forgotten by admin"
        node.decommissioned_at = now
        await on_node_boot(session, node.id)
        await resolve_node_alerts(session, node.id, now)
        record_audit(session, admin, "node.forget", "node", node.id, {"hostname": node.hostname})
        await session.commit()
    return {"id": str(node.id), "decommissioned_at": node.decommissioned_at.isoformat()}


@router.post("/nodes/{node_id}/meta")
async def set_node_meta(
    node_id: uuid.UUID,
    body: MetaIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set a PC's room and labels. A room set here is not overwritten by the
    agent's config; clearing it hands the room back to the agent."""
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    if "room" in body.model_fields_set:
        node.room = body.room
        node.room_source = "admin" if body.room else None
    if body.labels is not None:
        node.labels = body.labels
    record_audit(
        session, admin, "node.meta", "node", node.id,
        {"hostname": node.hostname, "room": node.room, "labels": node.labels or []},
    )
    await session.commit()
    return _serialize_node(node)


@router.post("/nodes/{node_id}/release")
async def release_node(
    node_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Return a drained or quarantined node to service."""
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    cred = await session.get(NodeCredential, node_id)
    if cred is not None and cred.revoked_at is not None:
        raise AppError(409, "conflict", "credential revoked; reinstall the PC with --reset-identity")
    node.eligibility = "eligible"
    node.eligibility_reason = None
    record_audit(session, admin, "node.release", "node", node.id, {"hostname": node.hostname})
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility}


@router.post("/nodes/{node_id}/ring")
async def set_node_ring(
    node_id: uuid.UUID,
    body: RingIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    previous = node.update_ring
    node.update_ring = body.ring
    record_audit(
        session, admin, "node.ring", "node", node.id,
        {"hostname": node.hostname, "ring": body.ring, "previous": previous},
    )
    await session.commit()
    return {"id": str(node.id), "update_ring": node.update_ring}


@router.post("/nodes/{node_id}/drain")
async def drain_node(
    node_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    node.eligibility = "draining"
    node.eligibility_reason = "drained by admin"
    record_audit(session, admin, "node.drain", "node", node.id, {"hostname": node.hostname})
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility}


@router.post("/nodes/{node_id}/quarantine")
async def quarantine_node(
    node_id: uuid.UUID,
    body: QuarantineIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.scheduler import on_node_boot

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    node.eligibility = "quarantined"
    node.eligibility_reason = body.reason
    await on_node_boot(session, node.id)
    record_audit(
        session, admin, "node.quarantine", "node", node.id, {"hostname": node.hostname, "reason": body.reason}
    )
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility, "reason": body.reason}
