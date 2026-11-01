"""People and quotas (B18): project members, the admin user list with
block / unblock, and concurrency quotas. Every mutation is audited (B21)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import record_audit
from app.config import settings
from app.db import get_session
from app.deps import get_current_user, require_admin, require_project_member
from app.errors import AppError
from app.identity import INVITE_PREFIX, is_invited
from app.models import Project, ProjectMember, Quota, User
from app.paging import decode_cursor, decode_offset, encode_cursor, encode_offset
from app.scheduler import db_now
from app.schemas import MemberRoleIn, QuotaIn

router = APIRouter()

QUOTA_SUBJECTS = ("user", "project")


def _default_quota(subject_type: str) -> int:
    return settings.default_user_concurrency if subject_type == "user" else settings.default_project_concurrency


# --- Project members --------------------------------------------------------------


def _serialize_member(member: ProjectMember, user: User) -> dict:
    return {
        "user_id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "role": member.role,
        "blocked": user.disabled_at is not None,
        "invited": is_invited(user),
    }


async def _project(session: AsyncSession, project_id: uuid.UUID) -> Project:
    project = await session.get(Project, project_id)
    if project is None:
        raise AppError(404, "not_found", "project not found", project_id=str(project_id))
    return project


async def _require_owner(project_id: uuid.UUID, actor: User, session: AsyncSession) -> None:
    member = await require_project_member(project_id, actor, session)
    if member.role != "owner" and not actor.is_admin:
        raise AppError(403, "forbidden", "only project owners can change members")


async def _owners(session: AsyncSession, project_id: uuid.UUID) -> list[ProjectMember]:
    """The project's owner rows, locked so two removals can not both take the last one."""
    return list(
        (
            await session.scalars(
                select(ProjectMember)
                .where(ProjectMember.project_id == project_id, ProjectMember.role == "owner")
                .with_for_update()
            )
        ).all()
    )


async def _quota_limit(session: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> int:
    row = await session.scalar(
        select(Quota).where(Quota.subject_type == subject_type, Quota.subject_id == subject_id)
    )
    return row.max_concurrent_jobs if row else _default_quota(subject_type)


@router.get("/projects/{project_id}/members")
async def list_members(
    project_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    caller = await require_project_member(project_id, user, session)
    project = await _project(session, project_id)
    rows = (
        await session.execute(
            select(ProjectMember, User)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.role.desc(), User.email)
        )
    ).all()
    return {
        "project": {
            "id": str(project.id),
            "name": project.name,
            "max_concurrent_jobs": await _quota_limit(session, "project", project.id),
        },
        "your_role": caller.role,
        "can_edit": caller.role == "owner" or user.is_admin,
        "items": [_serialize_member(m, u) for m, u in rows],
        "next_cursor": None,
    }


async def _member_row(session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID) -> tuple[ProjectMember, User]:
    row = (
        await session.execute(
            select(ProjectMember, User)
            .join(User, User.id == ProjectMember.user_id)
            .where(ProjectMember.project_id == project_id, ProjectMember.user_id == user_id)
        )
    ).first()
    if row is None:
        raise AppError(404, "not_found", "that user is not a member of this project")
    return row[0], row[1]


@router.patch("/projects/{project_id}/members/{user_id}")
async def change_member_role(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberRoleIn,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _require_owner(project_id, actor, session)
    owners = await _owners(session, project_id)
    member, target = await _member_row(session, project_id, user_id)
    previous = member.role
    if previous == "owner" and body.role != "owner" and len(owners) <= 1:
        raise AppError(409, "last_owner", "a project needs at least one owner; make someone else an owner first")
    member.role = body.role
    record_audit(
        session, actor, "member.role", "project", project_id,
        {"user_id": str(user_id), "email": target.email, "role": body.role, "previous": previous},
    )
    await session.commit()
    return _serialize_member(member, target)


@router.delete("/projects/{project_id}/members/{user_id}")
async def remove_member(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    actor: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _require_owner(project_id, actor, session)
    owners = await _owners(session, project_id)
    member, target = await _member_row(session, project_id, user_id)
    if member.role == "owner" and len(owners) <= 1:
        raise AppError(409, "last_owner", "a project needs at least one owner; make someone else an owner first")
    await session.delete(member)
    record_audit(
        session, actor, "member.remove", "project", project_id,
        {"user_id": str(user_id), "email": target.email, "role": member.role},
    )
    await session.commit()
    return {"user_id": str(user_id), "removed": True}


# --- Users --------------------------------------------------------------------------


async def _user_quotas(session: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not user_ids:
        return {}
    rows = (
        await session.scalars(
            select(Quota).where(Quota.subject_type == "user", Quota.subject_id.in_(user_ids))
        )
    ).all()
    return {row.subject_id: row.max_concurrent_jobs for row in rows}


def _serialize_user(user: User, quota: int | None) -> dict:
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "blocked": user.disabled_at is not None,
        "invited": is_invited(user),
        "disabled_at": user.disabled_at.isoformat() if user.disabled_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "max_concurrent_jobs": quota if quota is not None else _default_quota("user"),
    }


@router.get("/admin/users")
async def admin_users(
    q: str | None = None,
    blocked: bool | None = None,
    invited: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Users newest first. `q` matches the email or display name (any case);
    `invited=1` lists people added to a project who have not signed in yet."""
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc())
    if q and q.strip():
        term = q.strip()
        stmt = stmt.where(User.email.icontains(term, autoescape=True) | User.display_name.icontains(term, autoescape=True))
    if blocked is True:
        stmt = stmt.where(User.disabled_at.is_not(None))
    elif blocked is False:
        stmt = stmt.where(User.disabled_at.is_(None))
    if invited is True:
        stmt = stmt.where(User.university_sub.startswith(INVITE_PREFIX, autoescape=True))
    elif invited is False:
        stmt = stmt.where(~User.university_sub.startswith(INVITE_PREFIX, autoescape=True))
    if cursor:
        stmt = stmt.where(tuple_(User.created_at, User.id) < tuple_(*decode_cursor(cursor)))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    page = rows[:limit]
    quotas = await _user_quotas(session, [u.id for u in page])
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return {"items": [_serialize_user(u, quotas.get(u.id)) for u in page], "next_cursor": next_cursor}


async def _set_blocked(user_id: uuid.UUID, admin: User, session: AsyncSession, block: bool) -> dict:
    target = await session.get(User, user_id)
    if target is None:
        raise AppError(404, "not_found", "user not found")
    if block and target.id == admin.id:
        raise AppError(409, "conflict", "you can not block yourself")
    if block and target.disabled_at is None:
        target.disabled_at = await db_now(session)
    elif not block:
        target.disabled_at = None
    record_audit(session, admin, "user.block" if block else "user.unblock", "user", target.id, {"email": target.email})
    await session.commit()
    return _serialize_user(target, (await _user_quotas(session, [target.id])).get(target.id))


@router.post("/admin/users/{user_id}/block")
async def block_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every user endpoint answers 403 to a blocked user. Jobs already running finish."""
    return await _set_blocked(user_id, admin, session, True)


@router.post("/admin/users/{user_id}/unblock")
async def unblock_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _set_blocked(user_id, admin, session, False)


# --- Quotas --------------------------------------------------------------------------


async def _labels(session: AsyncSession, subject_type: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    if subject_type == "user":
        rows = (await session.execute(select(User.id, User.email).where(User.id.in_(ids)))).all()
    else:
        rows = (await session.execute(select(Project.id, Project.name).where(Project.id.in_(ids)))).all()
    return {row[0]: row[1] for row in rows}


def _quota_item(subject_type: str, subject_id: uuid.UUID, label: str | None, limit: int | None) -> dict:
    return {
        "subject_type": subject_type,
        "subject_id": str(subject_id),
        "label": label,
        "max_concurrent_jobs": limit if limit is not None else _default_quota(subject_type),
        "default": limit is None,
    }


def _check_subject(subject_type: str) -> None:
    if subject_type not in QUOTA_SUBJECTS:
        raise AppError(400, "invalid", "subject_type must be user or project")


async def _require_subject(session: AsyncSession, subject_type: str, subject_id: uuid.UUID) -> str:
    label = (await _labels(session, subject_type, [subject_id])).get(subject_id)
    if label is None:
        raise AppError(404, "not_found", f"{subject_type} not found")
    return label


@router.get("/admin/quotas")
async def admin_quotas(
    subject_type: str | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Concurrency quotas. With both filters this answers for one subject, the
    default (`"default": true`) if it has no row of its own."""
    if subject_type is not None:
        _check_subject(subject_type)
    if subject_id is not None and subject_type is None:
        raise AppError(400, "invalid", "subject_id needs subject_type")
    if subject_type and subject_id:
        label = await _require_subject(session, subject_type, subject_id)
        row = await session.scalar(
            select(Quota).where(Quota.subject_type == subject_type, Quota.subject_id == subject_id)
        )
        return {"items": [_quota_item(subject_type, subject_id, label, row.max_concurrent_jobs if row else None)], "next_cursor": None}

    stmt = select(Quota).order_by(Quota.subject_type, Quota.subject_id)
    if subject_type:
        stmt = stmt.where(Quota.subject_type == subject_type)
    offset = decode_offset(cursor) if cursor else 0
    rows = list((await session.scalars(stmt.offset(offset).limit(limit + 1))).all())
    page = rows[:limit]
    names: dict[uuid.UUID, str] = {}
    for kind in QUOTA_SUBJECTS:
        names |= await _labels(session, kind, [q.subject_id for q in page if q.subject_type == kind])
    return {
        "items": [_quota_item(q.subject_type, q.subject_id, names.get(q.subject_id), q.max_concurrent_jobs) for q in page],
        "next_cursor": encode_offset(offset + limit) if len(rows) > limit else None,
    }


@router.put("/admin/quotas/{subject_type}/{subject_id}")
async def set_quota(
    subject_type: str,
    subject_id: uuid.UUID,
    body: QuotaIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_subject(subject_type)
    label = await _require_subject(session, subject_type, subject_id)
    row = await session.scalar(
        select(Quota).where(Quota.subject_type == subject_type, Quota.subject_id == subject_id).with_for_update()
    )
    previous = row.max_concurrent_jobs if row else None
    if row is None:
        session.add(Quota(subject_type=subject_type, subject_id=subject_id, max_concurrent_jobs=body.max_concurrent_jobs))
    else:
        row.max_concurrent_jobs = body.max_concurrent_jobs
    record_audit(
        session, admin, "quota.set", subject_type, subject_id,
        {"label": label, "max_concurrent_jobs": body.max_concurrent_jobs, "previous": previous},
    )
    await session.commit()
    return _quota_item(subject_type, subject_id, label, body.max_concurrent_jobs)


# --- Projects (admin view) ------------------------------------------------------------


@router.get("/admin/projects")
async def admin_projects(
    q: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every project by name, for pickers and filters. `q` matches the name (any case)."""
    offset = decode_offset(cursor) if cursor else 0
    members = (
        select(ProjectMember.project_id, func.count().label("members"))
        .group_by(ProjectMember.project_id)
        .subquery()
    )
    stmt = (
        select(Project, User.email, func.coalesce(members.c.members, 0), Quota.max_concurrent_jobs)
        .join(User, User.id == Project.created_by)
        .outerjoin(members, members.c.project_id == Project.id)
        .outerjoin(Quota, (Quota.subject_type == "project") & (Quota.subject_id == Project.id))
        .order_by(func.lower(Project.name), Project.id)
        .offset(offset)
        .limit(limit + 1)
    )
    if q and q.strip():
        stmt = stmt.where(Project.name.icontains(q.strip(), autoescape=True))
    rows = (await session.execute(stmt)).all()
    return {
        "items": [
            {
                "id": str(project.id),
                "name": project.name,
                "created_by_email": email,
                "members": int(count),
                "max_concurrent_jobs": quota if quota is not None else _default_quota("project"),
            }
            for project, email, count, quota in rows[:limit]
        ],
        "next_cursor": encode_offset(offset + limit) if len(rows) > limit else None,
    }
