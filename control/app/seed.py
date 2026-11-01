from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import (
    PYTHON_CPU_IMAGE,
    PYTORCH_IMAGE,
    TENSORFLOW_IMAGE,
    TENSORFLOW_PLACEHOLDER_IMAGE,
)
from app.config import settings
from app.models import Environment, Quota, User

PYTORCH_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
# Compute capabilities the catalog images are built and tested for.
CATALOG_GPU_ARCHS = ("8.9", "8.6", "7.5", "cpu")
TENSORFLOW_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
PYTHON_CPU_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


def _add_archs(env: Environment) -> None:
    # Existing databases keep their rows; widen them when the catalog grows.
    missing = [a for a in CATALOG_GPU_ARCHS if a not in (env.gpu_archs or [])]
    if missing:
        env.gpu_archs = list(env.gpu_archs or []) + missing


async def seed(session: AsyncSession) -> None:
    pytorch = await session.get(Environment, PYTORCH_ID)
    if pytorch is None:
        session.add(
            Environment(
                id=PYTORCH_ID,
                name="pytorch-stable-cu124",
                framework="pytorch",
                python_version="3.12",
                cuda_version="12.4",
                gpu_archs=list(CATALOG_GPU_ARCHS),
                image_digest=PYTORCH_IMAGE,
                lockfile=None,
                cuda_preflight_ok=True,
            )
        )
    else:
        if pytorch.image_digest.startswith("sha256:dev"):
            pytorch.image_digest = PYTORCH_IMAGE
        _add_archs(pytorch)

    tensorflow = await session.get(Environment, TENSORFLOW_ID)
    if tensorflow is None:
        session.add(
            Environment(
                id=TENSORFLOW_ID,
                name="tensorflow-stable",
                framework="tensorflow",
                python_version="3.12",
                cuda_version="12.4",
                gpu_archs=list(CATALOG_GPU_ARCHS),
                image_digest=TENSORFLOW_IMAGE,
                lockfile=None,
                cuda_preflight_ok=True,
            )
        )
    else:
        # Only the old placeholder is replaced; an image an admin pinned stays.
        if tensorflow.image_digest.startswith("sha256:dev") or (
            tensorflow.image_digest == TENSORFLOW_PLACEHOLDER_IMAGE
        ):
            tensorflow.image_digest = TENSORFLOW_IMAGE
            tensorflow.upstream_image = None
        elif tensorflow.upstream_image == TENSORFLOW_PLACEHOLDER_IMAGE:
            # Mirrored while it was still the placeholder: mirror the real image instead.
            tensorflow.image_digest = TENSORFLOW_IMAGE
            tensorflow.upstream_image = None
        _add_archs(tensorflow)

    if await session.get(Environment, PYTHON_CPU_ID) is None:
        session.add(
            Environment(
                id=PYTHON_CPU_ID,
                name="python-cpu",
                framework="python",
                python_version="3.12",
                cuda_version="none",
                gpu_archs=["cpu"],
                image_digest=PYTHON_CPU_IMAGE,
                lockfile=None,
                cuda_preflight_ok=True,
            )
        )
    await session.commit()


async def ensure_user_quota(session: AsyncSession, user: User) -> None:
    row = await session.scalar(
        select(Quota).where(Quota.subject_type == "user", Quota.subject_id == user.id)
    )
    if row is None:
        session.add(
            Quota(
                subject_type="user",
                subject_id=user.id,
                max_concurrent_jobs=settings.default_user_concurrency,
            )
        )


async def ensure_project_quota(session: AsyncSession, project_id: uuid.UUID) -> None:
    row = await session.scalar(
        select(Quota).where(Quota.subject_type == "project", Quota.subject_id == project_id)
    )
    if row is None:
        session.add(
            Quota(
                subject_type="project",
                subject_id=project_id,
                max_concurrent_jobs=settings.default_project_concurrency,
            )
        )
