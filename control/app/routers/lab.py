"""Lab policy (B17, B19, B20, B21): class-hour schedules, catalog management,
rollout settings and the audit log. Every mutation here is audited."""
from __future__ import annotations

import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings
from app.agent_bundle import current_bundle, parse_version, target_version
from app.audit import ACTIONS, record_audit
from app.config import settings
from app.db import get_session
from app.deps import require_admin
from app.envbuild import is_registry_digest
from app.errors import AppError
from app.models import AuditLog, AvailabilityWindow, Environment, Job, Node, User
from app.paging import decode_cursor, encode_cursor
from app.schedule import hhmm, zone_name
from app.scheduler import db_now
from app.schemas import EnvironmentIn, EnvironmentPatch, RolloutIn, ScheduleIn, SchedulePatch
from app.usage import parse_moment

router = APIRouter()


# --- Schedules (B17) -----------------------------------------------------------------


def _serialize_window(row: AvailabilityWindow, hostname: str | None) -> dict:
    return {
        "id": str(row.id),
        "room": row.room,
        "node_id": str(row.node_id) if row.node_id else None,
        "hostname": hostname,
        "weekday": row.weekday,
        "start_minute": row.start_minute,
        "end_minute": row.end_minute,
        "start": hhmm(row.start_minute),
        "end": hhmm(row.end_minute),
        "note": row.note,
    }


async def _hostnames(session: AsyncSession, rows: list[AvailabilityWindow]) -> dict[uuid.UUID, str]:
    ids = {r.node_id for r in rows if r.node_id}
    if not ids:
        return {}
    found = (await session.execute(select(Node.id, Node.hostname).where(Node.id.in_(ids)))).all()
    return {row[0]: row[1] for row in found}


async def _checked(session: AsyncSession, row: AvailabilityWindow) -> None:
    """The rules every window follows, whether it was just made or just edited."""
    if (row.room is None) == (row.node_id is None):
        raise AppError(400, "invalid", "set exactly one of room and node_id")
    if row.end_minute <= row.start_minute:
        raise AppError(
            400, "invalid",
            "a window must end after it starts and can not cross midnight; add one window for each day",
        )
    if row.node_id is not None and await session.get(Node, row.node_id) is None:
        raise AppError(404, "not_found", "node not found")


def _audit_window(row: AvailabilityWindow, hostname: str | None) -> dict:
    return {
        "room": row.room,
        "node": hostname,
        "weekday": row.weekday,
        "start": hhmm(row.start_minute),
        "end": hhmm(row.end_minute),
        "note": row.note,
    }


@router.get("/admin/schedules")
async def list_schedules(
    room: str | None = None,
    node_id: uuid.UUID | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The closed windows, in `timezone` (LAB_TIMEZONE); weekday 0 is Monday."""
    stmt = select(AvailabilityWindow).order_by(
        AvailabilityWindow.weekday, AvailabilityWindow.start_minute, AvailabilityWindow.room
    )
    if room and room.strip():
        stmt = stmt.where(func.lower(AvailabilityWindow.room) == room.strip().lower())
    if node_id:
        stmt = stmt.where(AvailabilityWindow.node_id == node_id)
    rows = list((await session.scalars(stmt)).all())
    hosts = await _hostnames(session, rows)
    return {
        "timezone": zone_name(),
        "items": [_serialize_window(r, hosts.get(r.node_id) if r.node_id else None) for r in rows],
        "next_cursor": None,
    }


@router.post("/admin/schedules", status_code=201)
async def create_schedule(
    body: ScheduleIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = AvailabilityWindow(**body.model_dump())
    await _checked(session, row)
    session.add(row)
    await session.flush()
    hosts = await _hostnames(session, [row])
    host = hosts.get(row.node_id) if row.node_id else None
    record_audit(session, admin, "schedule.create", "schedule", row.id, _audit_window(row, host))
    await session.commit()
    return _serialize_window(row, host)


async def _window_or_404(session: AsyncSession, schedule_id: uuid.UUID) -> AvailabilityWindow:
    row = await session.get(AvailabilityWindow, schedule_id)
    if row is None:
        raise AppError(404, "not_found", "schedule window not found")
    return row


@router.patch("/admin/schedules/{schedule_id}")
async def update_schedule(
    schedule_id: uuid.UUID,
    body: SchedulePatch,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _window_or_404(session, schedule_id)
    data = body.model_dump(exclude_unset=True)
    # Naming a room moves the window off its PC and the other way round.
    if data.get("room") is not None and "node_id" not in data:
        row.node_id = None
    if data.get("node_id") is not None and "room" not in data:
        row.room = None
    for key in ("room", "node_id", "note"):
        if key in data:
            setattr(row, key, data[key])
    for key in ("weekday", "start_minute", "end_minute"):
        if data.get(key) is not None:
            setattr(row, key, data[key])
    await _checked(session, row)
    hosts = await _hostnames(session, [row])
    host = hosts.get(row.node_id) if row.node_id else None
    record_audit(session, admin, "schedule.update", "schedule", row.id, _audit_window(row, host))
    await session.commit()
    return _serialize_window(row, host)


@router.delete("/admin/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await _window_or_404(session, schedule_id)
    hosts = await _hostnames(session, [row])
    host = hosts.get(row.node_id) if row.node_id else None
    record_audit(session, admin, "schedule.delete", "schedule", row.id, _audit_window(row, host))
    await session.delete(row)
    await session.commit()
    return {"id": str(schedule_id), "deleted": True}


# --- Catalog (B19) -------------------------------------------------------------------


def _serialize_environment(env: Environment, jobs_last_30d: int) -> dict:
    return {
        "id": str(env.id),
        "name": env.name,
        "framework": env.framework,
        "python_version": env.python_version,
        "cuda_version": env.cuda_version,
        "gpu_archs": list(env.gpu_archs or []),
        "image_digest": env.image_digest,
        "upstream_image": env.upstream_image,
        "cuda_preflight_ok": env.cuda_preflight_ok,
        "retired": env.retired_at is not None,
        "retired_at": env.retired_at.isoformat() if env.retired_at else None,
        "created_at": env.created_at.isoformat() if env.created_at else None,
        "jobs_last_30d": jobs_last_30d,
    }


async def _jobs_last_30d(session: AsyncSession, env_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not env_ids:
        return {}
    since = (await db_now(session)) - timedelta(days=30)
    rows = (
        await session.execute(
            select(Job.environment_id, func.count())
            .where(Job.environment_id.in_(env_ids), Job.created_at >= since)
            .group_by(Job.environment_id)
        )
    ).all()
    return {row[0]: int(row[1]) for row in rows}


@router.get("/admin/environments")
async def admin_environments(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The catalog including retired images. Per-user custom builds (`build-*`) are not listed."""
    rows = list(
        (
            await session.scalars(
                select(Environment).where(~Environment.name.like("build-%")).order_by(Environment.name)
            )
        ).all()
    )
    counts = await _jobs_last_30d(session, [e.id for e in rows])
    return {"items": [_serialize_environment(e, counts.get(e.id, 0)) for e in rows], "next_cursor": None}


async def _name_taken(session: AsyncSession, name: str, except_id: uuid.UUID | None = None) -> bool:
    stmt = select(Environment.id).where(func.lower(Environment.name) == name.lower())
    if except_id:
        stmt = stmt.where(Environment.id != except_id)
    return await session.scalar(stmt) is not None


def _check_catalog_name(name: str) -> None:
    if name.lower().startswith("build-"):
        raise AppError(400, "invalid", 'names starting with "build-" are reserved for custom builds')


@router.post("/admin/environments", status_code=201)
async def create_environment(
    body: EnvironmentIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _check_catalog_name(body.name)
    if await _name_taken(session, body.name):
        raise AppError(409, "conflict", "an environment with that name exists")
    env = Environment(**body.model_dump(), cuda_preflight_ok=True)
    session.add(env)
    await session.flush()
    record_audit(
        session, admin, "environment.create", "environment", env.id,
        {"name": env.name, "image": env.image_digest, "gpu_archs": list(env.gpu_archs)},
    )
    await session.commit()
    return _serialize_environment(env, 0)


@router.patch("/admin/environments/{environment_id}")
async def update_environment(
    environment_id: uuid.UUID,
    body: EnvironmentPatch,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    env = await session.get(Environment, environment_id)
    if env is None or env.name.startswith("build-"):
        raise AppError(404, "not_found", "environment not found")
    data = body.model_dump(exclude_unset=True)
    retired = data.pop("retired", None)
    if data.get("name") is not None:
        _check_catalog_name(data["name"])
        if await _name_taken(session, data["name"], env.id):
            raise AppError(409, "conflict", "an environment with that name exists")
    changes: dict[str, dict] = {}
    for key, value in data.items():
        if value is None or getattr(env, key) == value:
            continue
        changes[key] = {"from": getattr(env, key), "to": value}
        setattr(env, key, value)
    if "image_digest" in changes and not is_registry_digest(env.image_digest):
        # A new public image: forget the old source so the mirror build uses this one.
        env.upstream_image = None
    action = "environment.update"
    if retired is True and env.retired_at is None:
        env.retired_at = await db_now(session)
        action = "environment.retire"
    elif retired is False and env.retired_at is not None:
        env.retired_at = None
        action = "environment.restore"
    if changes or action != "environment.update":
        record_audit(session, admin, action, "environment", env.id, {"name": env.name, "changes": changes})
    await session.commit()
    counts = await _jobs_last_30d(session, [env.id])
    return _serialize_environment(env, counts.get(env.id, 0))


# --- Rollout (B20) -------------------------------------------------------------------


async def _rollout_view(session: AsyncSession) -> dict:
    await platform_settings.ensure_loaded(session)
    bundle = current_bundle()
    stored = platform_settings.overrides()
    counts = (
        await session.execute(
            select(Node.agent_version, func.count())
            .where(Node.decommissioned_at.is_(None))
            .group_by(Node.agent_version)
        )
    ).all()
    return {
        "target_version": target_version(),
        "bundle_version": bundle.version if bundle else None,
        "rollout_ring": platform_settings.rollout_ring(),
        "min_version": platform_settings.min_version() or None,
        "overrides": {key: stored.get(key) for key in platform_settings.KEYS},
        "defaults": {
            "target_version": settings.agent_target_version or None,
            "rollout_ring": settings.agent_rollout_ring,
            "min_version": settings.agent_min_version or None,
        },
        "nodes_by_version": {version: int(n) for version, n in sorted(counts)},
    }


@router.get("/admin/rollout")
async def get_rollout(
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Effective agent rollout settings, the override behind each (null = the
    environment default applies) and how many PCs run each agent version."""
    return await _rollout_view(session)


def _version_or_clear(field: str, raw: str | None) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    if parse_version(text) is None:
        raise AppError(400, "invalid", f"{field} must look like 1.2.3")
    return text


@router.put("/admin/rollout")
async def put_rollout(
    body: RolloutIn,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sent = body.model_fields_set
    if not sent:
        raise AppError(400, "invalid", "send at least one of target_version, rollout_ring, min_version")
    await platform_settings.ensure_loaded(session)
    before = platform_settings.overrides()
    values: dict[str, object] = {}
    if "target_version" in sent:
        values["agent_target_version"] = _version_or_clear("target_version", body.target_version)
    if "min_version" in sent:
        values["agent_min_version"] = _version_or_clear("min_version", body.min_version)
    if "rollout_ring" in sent:
        values["agent_rollout_ring"] = body.rollout_ring
    await platform_settings.set_overrides(session, values, admin.email)
    record_audit(
        session, admin, "rollout.update", "rollout", None,
        {"changes": {key.removeprefix("agent_"): {"from": before.get(key), "to": value} for key, value in values.items()}},
    )
    await session.commit()
    platform_settings.clear_cache()
    return await _rollout_view(session)


# --- Audit log (B21) -----------------------------------------------------------------


def _serialize_audit(row: AuditLog) -> dict:
    return {
        "id": str(row.id),
        "at": row.at.isoformat(),
        "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "actor_email": row.actor_email,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "details": row.details or {},
    }


@router.get("/admin/audit")
async def admin_audit(
    action: str | None = None,
    actor: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = None,
    _admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Admin actions, newest first. `action` is exact (`node.drain`) or a family
    with a trailing star (`node.*`); `actor` matches the email in any case;
    `since` / `until` take an ISO date or time."""
    stmt = select(AuditLog).order_by(AuditLog.at.desc(), AuditLog.id.desc())
    if action and action.strip():
        wanted = action.strip()
        if wanted.endswith("*"):
            stmt = stmt.where(AuditLog.action.startswith(wanted[:-1], autoescape=True))
        else:
            stmt = stmt.where(AuditLog.action == wanted)
    if actor and actor.strip():
        stmt = stmt.where(AuditLog.actor_email.icontains(actor.strip(), autoescape=True))
    if target_type and target_type.strip():
        stmt = stmt.where(AuditLog.target_type == target_type.strip())
    if target_id and target_id.strip():
        stmt = stmt.where(AuditLog.target_id == target_id.strip())
    if since:
        stmt = stmt.where(AuditLog.at >= parse_moment(since))
    if until:
        stmt = stmt.where(AuditLog.at < parse_moment(until, end=True))
    if cursor:
        stmt = stmt.where(tuple_(AuditLog.at, AuditLog.id) < tuple_(*decode_cursor(cursor)))
    rows = list((await session.scalars(stmt.limit(limit + 1))).all())
    page = rows[:limit]
    return {
        "items": [_serialize_audit(r) for r in page],
        "next_cursor": encode_cursor(page[-1].at, page[-1].id) if len(rows) > limit else None,
        "actions": list(ACTIONS),
    }
