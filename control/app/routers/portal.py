from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.archive import ArchiveError, entrypoint_in_archive, inspect_archive
from app.config import settings
from app.db import SessionLocal, get_session
from app.deps import get_current_user, get_job_for_user, require_admin, require_project_member
from app.envbuild import resolve_environment
from app.errors import AppError
from app.models import (
    EnvironmentBuild,
    Artifact,
    Environment,
    Job,
    Node,
    Project,
    ProjectMember,
    User,
)
from app.scheduler import request_cancel
from app.schemas import MemberAdd, ProjectCreate, QuarantineIn
from app.seed import ensure_project_quota, ensure_user_quota
from app.serialize import load_job, node_for_job, serialize_job
from app.status import derived_status, latest_attempt
from app.storage import sha256_bytes, storage

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
    other = await session.scalar(select(User).where(User.email == email))
    if other is None:
        other = User(
            university_sub=f"invite:{email}",
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
        return {"user_id": str(other.id), "role": existing.role}
    session.add(ProjectMember(project_id=project_id, user_id=other.id, role=body.role))
    await session.commit()
    return {"user_id": str(other.id), "role": body.role}


@router.get("/environments")
async def list_environments(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await session.scalars(
            select(Environment)
            .where(~Environment.name.like("build-%"))
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


@router.post("/jobs", status_code=201)
async def create_job(
    project_id: uuid.UUID = Form(),
    entrypoint: str = Form(),
    environment_id: uuid.UUID | None = Form(default=None),
    requirements: str = Form(default=""),
    args: str = Form(default="[]"),
    resources: str = Form(default="{}"),
    archive: UploadFile = File(),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await require_project_member(project_id, user, session)
    if not entrypoint.endswith(".py"):
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
        names = inspect_archive(data, archive.filename or "input.tar.gz")
        resolved = entrypoint_in_archive(names, entrypoint)
    except ArchiveError as exc:
        raise AppError(400, "invalid", str(exc)) from exc

    runtime = int(res.get("max_runtime_seconds") or 3600)
    if runtime <= 0 or runtime > settings.global_max_runtime_seconds:
        raise AppError(400, "invalid", "max_runtime_seconds out of range")
    min_vram = int(res.get("min_vram_mb") or 0)
    if min_vram > 24576 and not user.is_admin:
        raise AppError(400, "invalid", "min_vram_mb exceeds v1 cap")

    job = Job(
        user_id=user.id,
        project_id=project_id,
        entrypoint=resolved,
        args=[str(a) for a in arg_list],
        environment_id=env.id,
        resources={
            "gpu_class": res.get("gpu_class"),
            "min_vram_mb": min_vram,
            "min_compute_capability": res.get("min_compute_capability"),
            "cpu_cores": int(res.get("cpu_cores") or 1),
            "ram_mb": int(res.get("ram_mb") or 512),
            "disk_mb": int(res.get("disk_mb") or 1024),
            "max_runtime_seconds": runtime,
        },
        max_runtime_seconds=runtime,
        pending_reason=(
            "building environment"
            if build is not None and build.state != "ready"
            else ("waiting for a matching GPU" if min_vram else "waiting for a matching node")
        ),
    )
    session.add(job)
    await session.flush()
    key = f"submissions/{job.id}/input.tar.gz"
    checksum = sha256_bytes(data)
    try:
        storage.put_bytes(key, data, "application/gzip")
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
    return serialize_job(job, await node_for_job(session, job))


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    job = await load_job(session, job_id)
    await require_project_member(job.project_id, user, session)
    job = await request_cancel(session, job_id)
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
    env = await session.get(Environment, job.environment_id)
    if env and not env.cuda_preflight_ok and str(env.name).startswith("build-"):
        env_hash = env.name.removeprefix("build-")
        build = await session.scalar(
            select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == env_hash)
        )
        if build is not None and build.state == "failed":
            build.state = "pending"
            build.claimed_node_id = None
            build.started_at = None
            build.finished_at = None
            build.error_message = None
        job.pending_reason = "building environment"
    else:
        job.pending_reason = "waiting for a matching node"
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


@router.get("/nodes")
async def list_nodes(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (await session.scalars(select(Node).order_by(Node.hostname))).all()
    return {
        "nodes": [
            {
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
                "cpu_cores": n.cpu_cores,
                "ram_mb": n.ram_mb,
            }
            for n in rows
        ]
    }


@router.post("/nodes/{node_id}/drain")
async def drain_node(
    node_id: uuid.UUID,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    node.eligibility = "draining"
    node.eligibility_reason = "drained by admin"
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility}


@router.post("/nodes/{node_id}/quarantine")
async def quarantine_node(
    node_id: uuid.UUID,
    body: QuarantineIn,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from app.scheduler import on_node_boot

    node = await session.get(Node, node_id)
    if node is None:
        raise AppError(404, "not_found", "node not found")
    node.eligibility = "quarantined"
    node.eligibility_reason = body.reason
    await on_node_boot(session, node.id)
    await session.commit()
    return {"id": str(node.id), "eligibility": node.eligibility, "reason": body.reason}
