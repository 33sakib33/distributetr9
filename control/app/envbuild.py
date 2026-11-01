from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import PYTORCH_IMAGE
from app.config import settings
from app.models import Artifact, Environment, EnvironmentBuild, Job, Node
from app.seed import PYTORCH_ID
from app.storage import storage

_REQ_LINE = re.compile(r"^\s*([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)\s*([><=!~].+)?\s*$")
_REGISTRY_DIGEST = re.compile(r"^[^@\s]+/[^@\s]+@sha256:[0-9a-f]{64}$", re.IGNORECASE)
LOCAL_DIGEST_ERROR = (
    "image_digest must be a registry digest (repo/name@sha256:64hex), "
    "not a local docker id"
)


def normalize_requirements(raw: str) -> list[str]:
    lines: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-"):
            raise ValueError("requirements.txt flags like -r or --index-url are not allowed")
        match = _REQ_LINE.match(stripped)
        if not match:
            raise ValueError(f"could not parse requirement: {stripped}")
        name = match.group(1).strip()
        spec = (match.group(2) or "").strip().replace(" ", "")
        lines.append((name + spec).lower())
    unique = sorted(set(lines))
    if not unique:
        raise ValueError("requirements.txt is empty")
    if len(unique) > 80:
        raise ValueError("too many packages (max 80)")
    return unique


def environment_hash(requirements: list[str], *, base_image: str, compat_class: str) -> str:
    payload = {
        "requirements": requirements,
        "base": base_image.strip(),
        "compat": (compat_class or "cpu").strip().lower(),
    }
    blob = __import__("json").dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def is_registry_digest(value: str | None) -> bool:
    if not value:
        return False
    return _REGISTRY_DIGEST.match(value.strip()) is not None


def image_ref_for(env_hash: str, *, registry_url: str | None = None) -> str:
    host = (settings.registry_url if registry_url is None else registry_url).strip().rstrip("/")
    name = "lab-env/" + env_hash[:16]
    if not host:
        return name
    return f"{host}/{name}"


def image_tag_for(env_hash: str) -> str:
    return image_ref_for(env_hash)


def on_current_registry(image_digest: str | None) -> bool:
    host = settings.registry_url.strip().rstrip("/")
    if not host or not image_digest:
        return True
    return image_digest.strip().split("/", 1)[0] == host


async def _rebuild_on_current_registry(
    session: AsyncSession, env: Environment, build: EnvironmentBuild
) -> None:
    # The hash does not include the registry, so a move (new LAN IP) leaves ready
    # builds pointing at a host lab PCs can no longer pull from.
    build.state = "pending"
    build.image_digest = None
    build.claimed_node_id = None
    build.started_at = None
    build.finished_at = None
    build.error_message = None
    env.image_digest = image_tag_for(build.environment_hash)
    env.cuda_preflight_ok = False
    jobs = (
        await session.scalars(
            select(Job).where(Job.environment_id == env.id, Job.accepted_attempt_id.is_(None))
        )
    ).all()
    for job in jobs:
        if job.desired_state != "cancel":
            job.pending_reason = "building environment"
            job.completed_at = None


async def resolve_environment(
    session: AsyncSession,
    *,
    requirements_text: str,
    gpu_class: str | None,
    base_env: Environment | None,
) -> tuple[Environment, EnvironmentBuild]:
    reqs = normalize_requirements(requirements_text)
    base_image = (base_env.image_digest if base_env else None) or PYTORCH_IMAGE
    compat = (gpu_class or "cpu").strip() or "cpu"
    env_hash = environment_hash(reqs, base_image=base_image, compat_class=compat)
    spec = "\n".join(reqs) + "\n"

    build = await session.scalar(
        select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == env_hash)
    )
    env = await session.scalar(select(Environment).where(Environment.name == f"build-{env_hash}"))
    if env is None:
        src = base_env or await session.get(Environment, PYTORCH_ID)
        env = Environment(
            name=f"build-{env_hash}",
            framework=(src.framework if src else "pytorch"),
            python_version=(src.python_version if src else "3.12"),
            cuda_version=(src.cuda_version if src else "12.4"),
            gpu_archs=list(src.gpu_archs) if src else ["cpu"],
            image_digest=image_tag_for(env_hash),
            lockfile=spec,
            cuda_preflight_ok=False,
        )
        session.add(env)
        await session.flush()
    if build is None:
        build = EnvironmentBuild(
            environment_hash=env_hash,
            state="pending",
            base_image=base_image,
            spec=spec,
            compat_class=compat,
        )
        session.add(build)
        await session.flush()
    if build.state == "ready" and not on_current_registry(build.image_digest or env.image_digest):
        await _rebuild_on_current_registry(session, env, build)
    if build.state == "ready" and (build.image_digest or env.image_digest):
        env.cuda_preflight_ok = True
        env.lockfile = build.lockfile or spec
        env.image_digest = build.image_digest or env.image_digest
    return env, build


async def attach_jobs_after_build(
    session: AsyncSession,
    build: EnvironmentBuild,
    *,
    ok: bool,
    image_digest: str | None,
    lockfile: str | None,
    error: str | None,
    log_text: str | None,
) -> None:
    now = datetime.now(timezone.utc)
    env = await session.scalar(
        select(Environment).where(Environment.name == f"build-{build.environment_hash}")
    )
    jobs: list[Job] = []
    if env is not None:
        jobs = (
            await session.scalars(
                select(Job).where(Job.environment_id == env.id, Job.accepted_attempt_id.is_(None))
            )
        ).all()
    if log_text:
        key = f"builds/{build.id}/log.txt"
        storage.put_bytes(key, log_text.encode(), "text/plain")
        checksum = hashlib.sha256(log_text.encode()).hexdigest()
        existing = await session.scalar(select(Artifact).where(Artifact.object_key == key))
        if existing is None:
            session.add(
                Artifact(
                    build_id=build.id,
                    type="log",
                    object_key=key,
                    size_bytes=len(log_text.encode()),
                    checksum_sha256=checksum,
                )
            )
        else:
            existing.size_bytes = len(log_text.encode())
            existing.checksum_sha256 = checksum
    if ok and image_digest:
        if not is_registry_digest(image_digest):
            ok = False
            error = LOCAL_DIGEST_ERROR
        else:
            build.state = "ready"
            build.image_digest = image_digest.strip()
            build.lockfile = lockfile or build.spec
            build.error_message = None
            build.finished_at = now
            if env is not None:
                env.cuda_preflight_ok = True
                env.image_digest = image_digest.strip()
                env.lockfile = build.lockfile
            for job in jobs:
                if job.desired_state == "cancel":
                    continue
                min_vram = int((job.resources or {}).get("min_vram_mb") or 0)
                job.pending_reason = (
                    "waiting for a matching GPU" if min_vram else "waiting for a matching node"
                )
                job.completed_at = None
            return
    build.state = "failed"
    build.error_message = (error or "environment build failed")[:2000]
    build.finished_at = now
    if env is not None:
        env.cuda_preflight_ok = False
    for job in jobs:
        if job.desired_state == "cancel":
            continue
        job.pending_reason = "environment build failed"
        job.completed_at = now


async def claim_build(session: AsyncSession, node: Node) -> EnvironmentBuild | None:
    from app.scheduler import is_train_node, node_can_claim

    if is_train_node(node):
        return None
    now = datetime.now(timezone.utc)
    if node_can_claim(node, now):
        return None
    stale = (
        await session.scalars(
            select(EnvironmentBuild).where(
                EnvironmentBuild.state == "building",
                EnvironmentBuild.started_at.is_not(None),
                EnvironmentBuild.started_at < now - timedelta(minutes=20),
            )
        )
    ).all()
    for row in stale:
        row.state = "pending"
        row.claimed_node_id = None
        row.started_at = None

    stmt = (
        select(EnvironmentBuild)
        .where(EnvironmentBuild.state == "pending")
        .order_by(EnvironmentBuild.created_at.asc())
        .with_for_update(skip_locked=True)
    )
    rows = (await session.scalars(stmt)).all()
    for build in rows:
        wanted = (build.compat_class or "cpu").strip()
        if wanted and wanted != "cpu" and wanted not in {node.compatibility_class, node.gpu_model}:
            continue
        build.state = "building"
        build.claimed_node_id = node.id
        build.started_at = now
        return build
    return None


def build_payload(build: EnvironmentBuild) -> dict:
    return {
        "build_id": str(build.id),
        "environment_hash": build.environment_hash,
        "base_image": build.base_image,
        "requirements": build.spec or "",
        "compat_class": build.compat_class or "cpu",
        "image_tag": image_tag_for(build.environment_hash),
        "need_cuda": (build.compat_class or "cpu") != "cpu",
    }


async def migrate_envbuild_columns(conn) -> None:
    statements = [
        "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS spec TEXT",
        "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS compat_class TEXT",
        "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS error_message TEXT",
        "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS claimed_node_id UUID",
        "ALTER TABLE environment_builds ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
    ]
    for stmt in statements:
        await conn.execute(text(stmt))
