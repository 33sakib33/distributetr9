from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.catalog import PYTORCH_IMAGE
from app.config import settings
from app.models import Artifact, Environment, EnvironmentBuild, Job, Node
from app.seed import PYTORCH_ID
from app.storage import storage

log = logging.getLogger("lab.envbuild")

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
    # The public image, not the mirrored one: the hash stays stable when a catalog
    # row is moved into the lab registry (or the registry moves).
    base_image = ((base_env.upstream_image or base_env.image_digest) if base_env else None) or PYTORCH_IMAGE
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
    if build.state == "expired":
        # Image was garbage-collected (app.ops gc); build it again on demand.
        await _rebuild_on_current_registry(session, env, build)
    if build.state == "ready" and (build.image_digest or env.image_digest):
        env.cuda_preflight_ok = True
        env.lockfile = build.lockfile or spec
        env.image_digest = build.image_digest or env.image_digest
    return env, build


async def restart_failed_build(session: AsyncSession, job: Job) -> bool:
    """For a job whose environment is a custom build that is not ready: put a
    failed build back in the queue. True when the job has to wait for the build."""
    env = await session.get(Environment, job.environment_id)
    if not (env and not env.cuda_preflight_ok and str(env.name).startswith("build-")):
        return False
    build = await session.scalar(
        select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == env.name.removeprefix("build-"))
    )
    if build is not None and build.state == "failed":
        build.state = "pending"
        build.claimed_node_id = None
        build.started_at = None
        build.finished_at = None
        build.error_message = None
    return True


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
    if build.kind == "mirror":
        await _finish_mirror(session, build, ok=ok, image_digest=image_digest, error=error, now=now)
        return
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
        if job.completed_at is None:
            metrics.job_finished(session, "failed")
        job.pending_reason = "environment build failed"
        job.completed_at = now


# A build that has been "building" this long lost its builder. Mirrors pull and
# push whole framework images, so they get far longer than a pip build.
_STALE_AFTER = {"pip": timedelta(minutes=20), "mirror": timedelta(minutes=90)}


async def claim_build(session: AsyncSession, node: Node) -> EnvironmentBuild | None:
    from app import platform_settings
    from app.schedule import load_schedule
    from app.scheduler import is_train_node, node_can_claim

    if is_train_node(node):
        return None
    now = datetime.now(timezone.utc)
    await platform_settings.ensure_loaded(session)
    if node_can_claim(node, now, await load_schedule(session)):
        return None
    building = (
        await session.scalars(
            select(EnvironmentBuild).where(
                EnvironmentBuild.state == "building",
                EnvironmentBuild.started_at.is_not(None),
            )
        )
    ).all()
    for row in building:
        if row.started_at < now - _STALE_AFTER.get(row.kind, _STALE_AFTER["pip"]):
            row.state = "pending"
            row.claimed_node_id = None
            row.started_at = None

    # Any builder serves every GPU class. People waiting on a pip build go before
    # the background mirrors of catalog images.
    stmt = (
        select(EnvironmentBuild)
        .where(EnvironmentBuild.state == "pending")
        .order_by((EnvironmentBuild.kind == "mirror").asc(), EnvironmentBuild.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    build = await session.scalar(stmt)
    if build is None:
        return None
    build.state = "building"
    build.claimed_node_id = node.id
    build.started_at = now
    return build


def build_payload(build: EnvironmentBuild) -> dict:
    if build.kind == "mirror":
        return {
            "build_id": str(build.id),
            "kind": "mirror",
            "source_image": build.base_image,
            "image_tag": build.spec or "",
        }
    return {
        "build_id": str(build.id),
        "kind": "pip",
        "environment_hash": build.environment_hash,
        "base_image": build.base_image,
        "requirements": build.spec or "",
        "compat_class": build.compat_class or "cpu",
        "image_tag": image_tag_for(build.environment_hash),
        "need_cuda": (build.compat_class or "cpu") != "cpu",
    }


# --- Mirror builds: catalog images served from the lab registry ---------------

_MIRROR_RETRY_AFTER = timedelta(minutes=5)


def mirror_hash(env: Environment) -> str:
    return hashlib.sha256(f"mirror:{env.id}".encode()).hexdigest()


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-.") or "image"


def mirror_tag_for(env: Environment, source_image: str, *, registry_url: str | None = None) -> str:
    """<registry>/lab/<env-name>:<tag of the source image>, with a valid docker tag."""
    host = (settings.registry_url if registry_url is None else registry_url).strip().rstrip("/")
    ref = source_image.strip().split("@", 1)[0]
    last = ref.rsplit("/", 1)[-1]
    tag = last.split(":", 1)[1] if ":" in last else "latest"
    tag = re.sub(r"[^A-Za-z0-9_.-]", "-", tag).lstrip(".-")[:128] or "latest"
    return f"{host}/lab/{_safe_name(env.name)}:{tag}"


def _is_mirrored(env: Environment) -> bool:
    digest = (env.image_digest or "").strip()
    return is_registry_digest(digest) and on_current_registry(digest)


async def ensure_mirror_builds(session: AsyncSession) -> int:
    """Queue a mirror build for every catalog environment not yet served from the
    current registry. Returns how many builds were created or re-queued."""
    registry = settings.registry_url.strip().rstrip("/")
    if not registry or not settings.mirror_catalog_images:
        return 0
    now = datetime.now(timezone.utc)
    envs = (
        await session.scalars(
            select(Environment)
            .where(~Environment.name.like("build-%"), Environment.retired_at.is_(None))
            .order_by(Environment.name)
        )
    ).all()
    queued = 0
    for env in envs:
        if _is_mirrored(env):
            continue
        source = (env.upstream_image or env.image_digest or "").strip()
        if not source or source.startswith("sha256:"):
            continue
        tag = mirror_tag_for(env, source, registry_url=registry)
        build = await session.scalar(
            select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == mirror_hash(env))
        )
        if build is None:
            session.add(
                EnvironmentBuild(
                    environment_hash=mirror_hash(env),
                    kind="mirror",
                    environment_id=env.id,
                    state="pending",
                    base_image=source,
                    spec=tag,
                    compat_class="cpu",
                )
            )
        elif build.state == "building":
            continue
        elif build.state == "pending":
            # Not claimed yet: follow a registry move or a changed source.
            build.base_image = source
            build.spec = tag
            continue
        elif build.state == "failed" and build.finished_at and now - build.finished_at < _MIRROR_RETRY_AFTER:
            continue
        else:
            build.state = "pending"
            build.base_image = source
            build.spec = tag
            build.image_digest = None
            build.claimed_node_id = None
            build.started_at = None
            build.finished_at = None
            build.error_message = None
        queued += 1
    if queued:
        await session.flush()
        log.info("queued %d catalog mirror build(s)", queued)
    return queued


async def _finish_mirror(
    session: AsyncSession,
    build: EnvironmentBuild,
    *,
    ok: bool,
    image_digest: str | None,
    error: str | None,
    now: datetime,
) -> None:
    env = await session.get(Environment, build.environment_id) if build.environment_id else None
    digest = (image_digest or "").strip()
    if ok and env is not None and is_registry_digest(digest) and on_current_registry(digest):
        if not env.upstream_image:
            env.upstream_image = env.image_digest
        env.image_digest = digest
        build.state = "ready"
        build.image_digest = digest
        build.error_message = None
        build.finished_at = now
        return
    if ok and not is_registry_digest(digest):
        error = LOCAL_DIGEST_ERROR
    elif ok:
        error = f"mirror digest is not on the current registry {settings.registry_url}: {digest}"
    # The catalog row keeps pointing at the image it had; jobs are unaffected.
    build.state = "failed"
    build.error_message = (error or "mirror build failed")[:2000]
    build.finished_at = now
    log.warning("mirror build for %s failed: %s", env.name if env else build.environment_id, build.error_message)
