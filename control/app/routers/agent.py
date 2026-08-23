from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.crypto import hash_token, new_boot_session, new_node_token
from app.db import get_session
from app.deps import get_current_node
from app.envbuild import attach_jobs_after_build, build_payload, claim_build
from app.errors import AppError
from app.logs import select_log_chunks
from app.models import Artifact, EnvironmentBuild, Job, Node, NodeCredential
from app.scheduler import (
    _get_attempt,
    _require_token,
    claim,
    claim_payload,
    complete,
    db_now,
    is_train_node,
    on_event,
    on_node_boot,
    renew,
)
from app.schemas import BuildCompleteIn, CompleteIn, EventIn, HeartbeatIn, LogsIn, RegisterIn, RenewIn
from app.storage import sha256_bytes, storage

router = APIRouter()


def _command_for(node: Node) -> str:
    if node.eligibility == "quarantined":
        return "quarantine"
    if node.eligibility == "draining":
        return "drain"
    return "continue"


@router.post("/agent/register")
async def register(
    body: RegisterIn,
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
) -> dict:
    token = new_node_token()
    boot = new_boot_session()
    now = await db_now(session)

    if body.node_id is None:
        node = Node(
            boot_session_id=boot,
            hostname=body.hostname,
            gpu_model=body.gpu_model,
            compatibility_class=body.compatibility_class,
            vram_total_mb=body.vram_total_mb,
            vram_allocatable_mb=body.vram_allocatable_mb,
            compute_capability=body.compute_capability,
            driver_version=body.driver_version,
            windows_version=body.windows_version,
            wsl_version=body.wsl_version,
            agent_version=body.agent_version,
            cpu_cores=body.cpu_cores,
            ram_mb=body.ram_mb,
            disk_free_bytes=body.disk_free_bytes,
            last_heartbeat_at=now,
            role=body.role,
            eligibility="eligible" if body.cuda_preflight_ok else "quarantined",
            eligibility_reason=None if body.cuda_preflight_ok else "cuda preflight failed",
        )
        session.add(node)
        await session.flush()
        session.add(NodeCredential(node_id=node.id, token_hash=hash_token(token)))
    else:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AppError(401, "auth", "existing node_id requires bearer token")
        presented = authorization.split(" ", 1)[1].strip()
        cred = await session.scalar(
            select(NodeCredential).where(
                NodeCredential.node_id == body.node_id,
                NodeCredential.revoked_at.is_(None),
            )
        )
        if cred is None or cred.token_hash != hash_token(presented):
            raise AppError(401, "auth", "invalid node token")
        node = await session.get(Node, body.node_id)
        if node is None:
            raise AppError(404, "not_found", "node not found")
        await on_node_boot(session, node.id)
        node.boot_session_id = boot
        node.hostname = body.hostname
        node.gpu_model = body.gpu_model
        node.compatibility_class = body.compatibility_class
        node.vram_total_mb = body.vram_total_mb
        node.vram_allocatable_mb = body.vram_allocatable_mb
        node.compute_capability = body.compute_capability
        node.driver_version = body.driver_version
        node.windows_version = body.windows_version
        node.wsl_version = body.wsl_version
        node.agent_version = body.agent_version
        node.cpu_cores = body.cpu_cores
        node.ram_mb = body.ram_mb
        node.disk_free_bytes = body.disk_free_bytes
        node.role = body.role
        node.last_heartbeat_at = now
        if node.eligibility == "draining":
            pass
        elif not body.cuda_preflight_ok:
            node.eligibility = "quarantined"
            node.eligibility_reason = "cuda preflight failed"
        elif node.eligibility == "quarantined" and node.eligibility_reason in {
            "cuda preflight failed",
            "agent unhealthy",
        }:
            node.eligibility = "eligible"
            node.eligibility_reason = None
        elif node.eligibility not in {"quarantined"}:
            node.eligibility = "eligible"
        token = presented
    await session.commit()
    return {
        "node_id": str(node.id),
        "node_token": token,
        "boot_session_id": boot,
        "lease_ttl_seconds": settings.lease_ttl_seconds,
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
        "disconnect_grace_seconds": settings.disconnect_grace_seconds,
        "role": node.role,
        "registry_url": settings.registry_url or None,
    }


@router.post("/agent/heartbeat")
async def heartbeat(
    body: HeartbeatIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    now = await db_now(session)
    node.last_heartbeat_at = now
    node.disk_free_bytes = max(body.disk_linux_free_bytes, body.disk_windows_free_bytes)
    node.gpu_desktop_busy = body.gpu_desktop_busy
    node.last_health = body.model_dump(mode="json")
    if body.agent_healthy is False:
        if node.eligibility not in {"draining", "quarantined"}:
            node.eligibility = "quarantined"
            node.eligibility_reason = "agent unhealthy"
    elif node.eligibility == "offline":
        node.eligibility = "eligible"
    elif node.eligibility == "quarantined" and node.eligibility_reason == "agent unhealthy":
        node.eligibility = "eligible"
        node.eligibility_reason = None
    await session.commit()
    return {
        "command": _command_for(node),
        "next_heartbeat_seconds": settings.heartbeat_interval_seconds,
    }


@router.post("/agent/claim")
async def claim_job(
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    import asyncio

    waited = 0.0
    while True:
        if is_train_node(node):
            attempt = await claim(session, node)
            if attempt:
                payload = await claim_payload(session, attempt)
                await session.commit()
                return {"attempt": payload, "build": None}
        else:
            build = await claim_build(session, node)
            if build:
                payload = build_payload(build)
                await session.commit()
                return {"attempt": None, "build": payload}
        await session.commit()
        if waited >= settings.claim_poll_seconds:
            return {"attempt": None, "build": None}
        await asyncio.sleep(1.0)
        waited += 1.0
        await session.refresh(node)


@router.post("/attempts/{attempt_id}/acknowledge")
async def acknowledge(
    attempt_id: uuid.UUID,
    body: EventIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt = await on_event(session, attempt_id, body.fencing_token, body.state or "starting")
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    await session.commit()
    return {"observed_state": attempt.observed_state}


@router.post("/attempts/{attempt_id}/renew")
async def renew_lease(
    attempt_id: uuid.UUID,
    body: RenewIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt, job = await renew(
        session, attempt_id, body.fencing_token, body.observed_state, body.resource_usage
    )
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    await session.commit()
    return {
        "lease_expires_at": attempt.lease_expires_at.isoformat(),
        "desired_state": job.desired_state,
        "observed_state": attempt.observed_state,
    }


@router.post("/attempts/{attempt_id}/events")
async def events(
    attempt_id: uuid.UUID,
    body: EventIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt = await on_event(session, attempt_id, body.fencing_token, body.state)
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    await session.commit()
    return {"observed_state": attempt.observed_state}


@router.post("/attempts/{attempt_id}/logs")
async def ingest_logs(
    attempt_id: uuid.UUID,
    body: LogsIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt = await _get_attempt(session, attempt_id)
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    _require_token(attempt, body.fencing_token)
    last = int(attempt.last_log_seq)
    last_chunk = body.chunks[-1].seq if body.chunks else body.first_seq - 1
    if body.first_seq == last + 1:
        pass
    elif body.first_seq <= last and last_chunk <= last:
        return {"last_seq": last}
    else:
        raise AppError(409, "log_gap", "log sequence gap", last_seq=last)

    accepted, usage = select_log_chunks(attempt, body.chunks)
    size = 0
    for chunk in accepted:
        encoded = json.dumps(chunk) + "\n"
        size += len(encoded)
        key = f"{attempt.output_namespace}logs/{chunk['seq']}.jsonl.gz"
        storage.put_log_chunk(key, encoded.encode())
    attempt.log_bytes = int(attempt.log_bytes or 0) + size
    attempt.last_log_seq = int(usage.get("last_seq") or last)
    attempt.resource_usage = usage
    flag_modified(attempt, "resource_usage")
    await session.commit()
    return {"last_seq": attempt.last_log_seq, "truncated": bool(usage.get("log_truncated"))}


@router.put("/attempts/{attempt_id}/outputs/{relative_path:path}")
async def upload_output(
    attempt_id: uuid.UUID,
    relative_path: str,
    request: Request,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
    x_fencing_token: uuid.UUID = Header(alias="X-Fencing-Token"),
    x_checksum_sha256: str = Header(alias="X-Checksum-Sha256"),
) -> dict:
    attempt = await _get_attempt(session, attempt_id)
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    _require_token(attempt, x_fencing_token)
    if ".." in relative_path.split("/") or relative_path.startswith("/"):
        raise AppError(400, "invalid", "output path must be relative")
    data = await request.body()
    checksum = sha256_bytes(data)
    if checksum != x_checksum_sha256.lower():
        raise AppError(400, "checksum_mismatch", "body checksum does not match header")
    if len(data) > settings.output_file_bytes:
        raise AppError(413, "resource", "single output file exceeds limit")
    usage = dict(attempt.resource_usage) if isinstance(attempt.resource_usage, dict) else {}
    output_bytes = int(usage.get("output_bytes") or 0) + len(data)
    if output_bytes > settings.output_bytes_per_attempt:
        raise AppError(413, "resource", "output size limit exceeded")
    key = f"{attempt.output_namespace}outputs/{relative_path}"
    storage.put_bytes(key, data)
    usage["output_bytes"] = output_bytes
    attempt.resource_usage = usage
    flag_modified(attempt, "resource_usage")
    await session.commit()
    return {"object_key": key, "size_bytes": len(data), "checksum_sha256": checksum}


@router.get("/attempts/{attempt_id}/input")
async def download_input(
    attempt_id: uuid.UUID,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
    x_fencing_token: uuid.UUID = Header(alias="X-Fencing-Token"),
):
    attempt = await _get_attempt(session, attempt_id)
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    _require_token(attempt, x_fencing_token)
    job = await session.get(Job, attempt.job_id)
    if job is None or job.submission_artifact_id is None:
        raise AppError(404, "not_found", "submission missing")
    artifact = await session.get(Artifact, job.submission_artifact_id)
    if artifact is None:
        raise AppError(404, "not_found", "submission missing")
    data = storage.get_bytes(artifact.object_key)
    return Response(content=data, media_type="application/gzip")


@router.post("/attempts/{attempt_id}/complete")
async def complete_attempt(
    attempt_id: uuid.UUID,
    body: CompleteIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
):
    payload = await complete(
        session,
        attempt_id,
        body.fencing_token,
        body.exit_code,
        body.failure_category,
        [item.model_dump() for item in body.output_manifest],
    )
    await session.commit()
    if payload.get("error") == "cancelled":
        return JSONResponse(status_code=409, content=payload)
    return payload


@router.post("/agent/builds/{build_id}/complete")
async def complete_build(
    build_id: uuid.UUID,
    body: BuildCompleteIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    build = await session.get(EnvironmentBuild, build_id)
    if build is None:
        raise AppError(404, "not_found", "build not found")
    if build.claimed_node_id is not None and build.claimed_node_id != node.id:
        raise AppError(403, "forbidden", "build is not on this node")
    if build.state == "ready":
        return {"state": build.state, "image_digest": build.image_digest}
    await attach_jobs_after_build(
        session,
        build,
        ok=body.ok,
        image_digest=body.image_digest,
        lockfile=body.lockfile,
        error=body.error,
        log_text=body.log,
    )
    await session.commit()
    return {"state": build.state, "image_digest": build.image_digest}


@router.post("/attempts/{attempt_id}/cleanup-complete")
async def cleanup_complete(
    attempt_id: uuid.UUID,
    body: CompleteIn,
    node: Node = Depends(get_current_node),
    session: AsyncSession = Depends(get_session),
) -> dict:
    attempt = await _get_attempt(session, attempt_id)
    if attempt.node_id != node.id:
        raise AppError(403, "forbidden", "attempt is not on this node")
    _require_token(attempt, body.fencing_token)
    await session.commit()
    return {"ok": True, "attempt_id": str(attempt_id)}
