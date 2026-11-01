from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.crypto import hash_token
from app.db import get_session
from app.errors import AppError
from app.identity import is_invited
from app.observability import note
from app.models import Job, Node, NodeCredential, ProjectMember, User
from app.seed import ensure_user_quota
from app.session_tokens import SESSION_PREFIX, read_session


async def _user_from_dev_header(session: AsyncSession, x_dev_user: str | None) -> User | None:
    if not settings.dev_auth or not x_dev_user or "@" not in x_dev_user:
        return None
    email = x_dev_user.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    if user is not None and is_invited(user):
        user.university_sub = f"dev:{email}"  # first sign-in claims the invite
        await session.commit()
    if user is None:
        user = User(
            university_sub=f"dev:{email}",
            email=email,
            display_name=email.split("@")[0],
            is_admin=settings.dev_auth_admin,
        )
        session.add(user)
        await session.flush()
        await ensure_user_quota(session, user)
        await session.commit()
        await session.refresh(user)
    return user


async def get_current_user(
    session: AsyncSession = Depends(get_session),
    x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
    authorization: str | None = Header(default=None),
    lab_session: str | None = Cookie(default=None),
) -> User:
    token = lab_session
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization.split(" ", 1)[1].strip()
        if raw.startswith(SESSION_PREFIX):
            token = raw
    if token:
        try:
            user_id = read_session(token)
        except ValueError:
            token = None
        else:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                raise AppError(401, "auth", "session user not found")
            if user.disabled_at is not None:
                raise AppError(403, "disabled", "user is disabled")
            note(user=user.email)
            return user

    user = await _user_from_dev_header(session, x_dev_user)
    if user is not None:
        if user.disabled_at is not None:
            raise AppError(403, "disabled", "user is disabled")
        note(user=user.email)
        return user

    raise AppError(401, "auth", "Please sign in")


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise AppError(403, "forbidden", "admin required")
    return user


async def require_project_member(
    project_id: uuid.UUID,
    user: User,
    session: AsyncSession,
) -> ProjectMember:
    member = await session.scalar(
        select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user.id,
        )
    )
    if member is None and not user.is_admin:
        raise AppError(403, "forbidden", "not a member of this project", project_id=str(project_id))
    if member is None:
        return ProjectMember(project_id=project_id, user_id=user.id, role="owner")
    return member


async def get_job_for_user(
    job_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Job:
    job = await session.scalar(
        select(Job).where(Job.id == job_id).options(selectinload(Job.attempts))
    )
    if job is None:
        raise AppError(404, "not_found", "job not found", job_id=str(job_id))
    await require_project_member(job.project_id, user, session)
    return job


async def get_current_node(
    session: AsyncSession = Depends(get_session),
    authorization: str | None = Header(default=None),
    x_node_id: str | None = Header(default=None, alias="X-Node-Id"),
    x_boot_session_id: str | None = Header(default=None, alias="X-Boot-Session-Id"),
) -> Node:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AppError(401, "auth", "bearer token required")
    token = authorization.split(" ", 1)[1].strip()
    token_h = hash_token(token)
    cred = await session.scalar(
        select(NodeCredential).where(
            NodeCredential.token_hash == token_h,
            NodeCredential.revoked_at.is_(None),
        )
    )
    if cred is None:
        raise AppError(401, "auth", "invalid node token")
    if x_node_id and str(cred.node_id) != x_node_id:
        raise AppError(401, "auth", "node id does not match token")
    node = await session.get(Node, cred.node_id)
    if node is None:
        raise AppError(401, "auth", "node not found")
    if x_boot_session_id and x_boot_session_id != node.boot_session_id:
        raise AppError(
            409,
            "stale_boot_session",
            "boot session mismatch; re-register",
            node_id=str(node.id),
        )
    note(node=node.hostname)
    return node
