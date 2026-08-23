from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.catalog import PYTORCH_IMAGE, TENSORFLOW_IMAGE
from app.config import settings
from app.models import Environment, Quota, User

PYTORCH_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
TENSORFLOW_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


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
                gpu_archs=["8.9", "8.6", "cpu"],
                image_digest=PYTORCH_IMAGE,
                lockfile=None,
                cuda_preflight_ok=True,
            )
        )
    elif pytorch.image_digest.startswith("sha256:dev"):
        pytorch.image_digest = PYTORCH_IMAGE

    tensorflow = await session.get(Environment, TENSORFLOW_ID)
    if tensorflow is None:
        session.add(
            Environment(
                id=TENSORFLOW_ID,
                name="tensorflow-stable",
                framework="tensorflow",
                python_version="3.12",
                cuda_version="12.4",
                gpu_archs=["8.9", "8.6", "cpu"],
                image_digest=TENSORFLOW_IMAGE,
                lockfile=None,
                cuda_preflight_ok=True,
            )
        )
    elif tensorflow.image_digest.startswith("sha256:dev"):
        tensorflow.image_digest = TENSORFLOW_IMAGE
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
