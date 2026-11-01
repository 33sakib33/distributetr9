"""What a student sees around a job (B22-B28): archive inspection, GPU
availability, notification settings, cloning a job and downloading its
outputs as one zip."""
from __future__ import annotations

import asyncio
import tempfile
import zipfile
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import availability
from app.archive import (
    ArchiveError,
    candidate_entrypoints,
    entrypoint_in_archive,
    find_requirements,
    inspect_archive,
    prepare_upload,
    suggest_entrypoint,
)
from app.config import settings
from app.db import get_session
from app.deps import get_current_user, get_job_for_user
from app.envbuild import restart_failed_build
from app.errors import AppError
from app.jobspec import job_resources, waiting_reason
from app.models import Artifact, Environment, Job, User
from app.notify import email_enabled, webhook_problem
from app.ratelimit import limit_user
from app.schemas import CloneIn, NotificationsIn
from app.serialize import load_job, node_for_job, serialize_job
from app.storage import storage

router = APIRouter()

SPOOL_BYTES = 32 * 1024 * 1024


# --- Upload inspection (B22) -------------------------------------------------------


@router.post(
    "/jobs/inspect",
    dependencies=[Depends(limit_user("inspect", "rate_limit_inspect_per_minute"))],
)
async def inspect_upload(
    archive: UploadFile = File(),
    _user: User = Depends(get_current_user),
) -> dict:
    """Look inside an upload before it is submitted: which scripts could be the
    entrypoint, the likeliest one, and the requirements.txt it came with."""
    data = await archive.read()
    if not data:
        raise AppError(400, "invalid", "archive is empty")
    if len(data) > settings.max_upload_bytes:
        raise AppError(400, "invalid", "archive exceeds size limit")
    try:
        upload = prepare_upload(data, archive.filename or "input.tar.gz")
        entrypoints = candidate_entrypoints(upload.names)
        suggested = upload.script or suggest_entrypoint(entrypoints)
        requirements = find_requirements(upload.data, archive.filename or "", upload.names, suggested)
    except ArchiveError as exc:
        raise AppError(400, "invalid", str(exc)) from exc
    return {"entrypoints": entrypoints, "suggested_entrypoint": suggested, "requirements": requirements}


# --- Availability (B23) ------------------------------------------------------------


@router.get("/availability")
async def get_availability(
    _user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return {"classes": await availability.availability(session)}


# --- Notifications (B26) -----------------------------------------------------------


def _notification_settings(user: User) -> dict:
    return {
        "notify_email": user.notify_email,
        "notify_webhook_url": user.notify_webhook_url,
        "email_available": email_enabled(),
    }


@router.get("/me/notifications")
async def get_notifications(user: User = Depends(get_current_user)) -> dict:
    return _notification_settings(user)


@router.put("/me/notifications")
async def put_notifications(
    body: NotificationsIn,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fields left out stay as they are; an empty webhook URL turns the webhook off."""
    if "notify_webhook_url" in body.model_fields_set and body.notify_webhook_url:
        problem = webhook_problem(body.notify_webhook_url)
        if problem:
            raise AppError(422, "invalid", problem)
    if body.notify_email is not None:
        user.notify_email = body.notify_email
    if "notify_webhook_url" in body.model_fields_set:
        user.notify_webhook_url = body.notify_webhook_url
    await session.commit()
    return _notification_settings(user)


# --- Clone (B27) -------------------------------------------------------------------


async def _clone_environment(
    session: AsyncSession, source: Job, wanted: Environment | None
) -> Environment:
    """The environment of the copy: the original's (a custom build included) or
    a catalog image the student picked instead."""
    if wanted is not None and wanted.id != source.environment_id:
        if wanted.name.startswith("build-") or not wanted.cuda_preflight_ok:
            raise AppError(400, "invalid", "unknown or untested environment")
        env = wanted
    else:
        env = await session.get(Environment, source.environment_id)
        if env is None:
            raise AppError(409, "conflict", "the job's environment no longer exists")
    if env.retired_at is not None:
        raise AppError(400, "invalid", f"environment {env.name} is retired; choose another")
    return env


@router.post("/jobs/{job_id}/clone", status_code=201)
async def clone_job(
    body: CloneIn | None = None,
    source: Job = Depends(get_job_for_user),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A new job in the same project, owned by the caller, that reuses the
    original's upload (copied) and environment. Resources, arguments, the
    entrypoint and the catalog environment can be changed."""
    body = body or CloneIn()
    submission = await session.get(Artifact, source.submission_artifact_id) if source.submission_artifact_id else None
    if submission is None:
        raise AppError(409, "conflict", "the job has no stored upload to copy")
    wanted_env = await session.get(Environment, body.environment_id) if body.environment_id else None
    if body.environment_id and wanted_env is None:
        raise AppError(400, "invalid", "unknown or untested environment")
    env = await _clone_environment(session, source, wanted_env)

    entrypoint = (body.entrypoint or "").strip() or source.entrypoint
    if body.entrypoint is not None and entrypoint != source.entrypoint:
        if not entrypoint.endswith(".py"):
            raise AppError(400, "invalid", "entrypoint must be a .py file")
        try:
            names = inspect_archive(storage.get_bytes(submission.object_key), submission.object_key)
            entrypoint = entrypoint_in_archive(names, entrypoint)
        except ArchiveError as exc:
            raise AppError(400, "invalid", str(exc)) from exc
    resources, runtime = job_resources({**(source.resources or {}), **(body.resources or {})}, user)

    job = Job(
        user_id=user.id,
        project_id=source.project_id,
        entrypoint=entrypoint,
        args=[str(a) for a in (source.args if body.args is None else body.args)],
        environment_id=env.id,
        resources=resources,
        max_runtime_seconds=runtime,
    )
    building = await restart_failed_build(session, job)
    job.pending_reason = "building environment" if building else waiting_reason(resources["min_vram_mb"])
    session.add(job)
    await session.flush()
    extension = submission.object_key.rsplit("input.", 1)[-1]
    key = f"submissions/{job.id}/input.{extension}"
    try:
        storage.copy(submission.object_key, key)
    except Exception as exc:
        await session.rollback()
        raise AppError(503, "storage", "failed to copy the submission") from exc
    artifact = Artifact(
        job_id=job.id,
        type="input",
        object_key=key,
        size_bytes=submission.size_bytes,
        checksum_sha256=submission.checksum_sha256,
    )
    session.add(artifact)
    await session.flush()
    job.submission_artifact_id = artifact.id
    await session.commit()
    job = await load_job(session, job.id)
    return serialize_job(job, await node_for_job(session, job))


# --- Outputs as one zip (B28) ------------------------------------------------------


def _member_name(relative: str) -> str:
    parts = [p for p in relative.split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def _build_zip(entries: list[tuple[str, str]]) -> tempfile.SpooledTemporaryFile:
    """Write the objects into a zip held in memory up to SPOOL_BYTES, on disk beyond."""
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
    now = datetime.now(timezone.utc).timetuple()[:6]
    try:
        with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as archive:
            for key, name in entries:
                info = zipfile.ZipInfo(name, date_time=now)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                with archive.open(info, "w", force_zip64=True) as member:
                    for chunk in storage.iter_bytes(key):
                        member.write(chunk)
    except Exception:
        spool.close()
        raise
    return spool


def _stream(spool: tempfile.SpooledTemporaryFile, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    try:
        spool.seek(0)
        while chunk := spool.read(chunk_size):
            yield chunk
    finally:
        spool.close()


@router.get("/jobs/{job_id}/outputs.zip")
async def download_outputs(
    job: Job = Depends(get_job_for_user),
    session: AsyncSession = Depends(get_session),
):
    """Every file the job promoted, paths relative to its outputs folder."""
    prefix = f"jobs/{job.id}/promoted/"
    rows = (
        await session.scalars(
            select(Artifact).where(Artifact.job_id == job.id, Artifact.type == "output").order_by(Artifact.object_key)
        )
    ).all()
    entries: dict[str, str] = {}
    for row in rows:
        name = _member_name(row.object_key.removeprefix(prefix))
        if name and name not in entries.values():
            entries[row.object_key] = name
    if not entries:
        raise AppError(404, "not_found", "this job has no outputs")
    try:
        spool = await asyncio.to_thread(_build_zip, list(entries.items()))
    except Exception as exc:
        raise AppError(503, "storage", "could not read the job's outputs") from exc
    size = spool.seek(0, 2)
    return StreamingResponse(
        _stream(spool),
        media_type="application/zip",
        headers={
            "Content-Length": str(size),
            "Content-Disposition": f'attachment; filename="job-{str(job.id)[:8]}-outputs.zip"',
        },
    )

