"""Operational alerts: the conditions an admin must hear about without watching
the portal. `check()` builds the current list; `sync_alerts()` keeps the
`alerts` table in step with it (open, resolved, re-opened); the alert worker
then posts the ones that are new, unmuted and unacknowledged to
ALERT_WEBHOOK_URL (Slack/Teams/Discord-style JSON)."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AppError
from app.models import AlertRecord, Job, JobAttempt, Node
from app.scheduler import db_now, is_train_node

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
# How long a resolved alert stays visible with ?include_resolved=1.
RESOLVED_VISIBLE = timedelta(hours=24)
_ALERT_LOCK = 7_340_001  # pg_advisory_xact_lock key: one alert sync at a time


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str

    @property
    def kind(self) -> str:
        return kind_of(self.key)


def kind_of(key: str) -> str:
    """`offline:<node id>` -> `offline`; keys without a subject are their own kind."""
    return key.split(":", 1)[0]


async def check(session: AsyncSession) -> list[Alert]:
    now = await db_now(session)
    alerts: list[Alert] = []

    nodes = (await session.scalars(select(Node).where(Node.decommissioned_at.is_(None)))).all()
    offline_after = timedelta(seconds=settings.alert_offline_seconds)
    for node in nodes:
        if node.eligibility == "quarantined":
            continue
        seen = node.last_heartbeat_at
        if seen is not None and now - seen > offline_after and now - seen < timedelta(days=7):
            alerts.append(Alert(f"offline:{node.id}", "warning", f"{node.hostname} offline since {seen:%Y-%m-%d %H:%M}"))
        if is_train_node(node) and node.eligibility == "eligible" and node.disk_free_bytes < settings.disk_watermark_bytes * 2:
            gb = node.disk_free_bytes / 1024**3
            alerts.append(Alert(f"disk:{node.id}", "warning", f"{node.hostname} low on disk ({gb:.1f} GB free)"))

    hour_ago = now - timedelta(hours=1)
    lost = await session.scalar(
        select(func.count()).select_from(JobAttempt).where(
            JobAttempt.failure_category.in_(("lease_lost", "node_lost")),
            JobAttempt.completed_at > hour_ago,
        )
    )
    if int(lost or 0) >= settings.alert_lease_losses_per_hour:
        alerts.append(Alert("lease_losses", "warning", f"{lost} attempts lost their node in the last hour"))

    flaky = (
        await session.execute(
            select(Node.hostname, func.count())
            .join(JobAttempt, JobAttempt.node_id == Node.id)
            .where(
                Node.decommissioned_at.is_(None),
                JobAttempt.observed_state == "failed",
                JobAttempt.failure_category.in_(("environment", "internal", "node_lost")),
                JobAttempt.completed_at > now - timedelta(hours=6),
            )
            .group_by(Node.hostname)
        )
    ).all()
    for hostname, count in flaky:
        if count >= settings.alert_node_failures:
            alerts.append(Alert(f"failures:{hostname}", "warning", f"{hostname} failed {count} attempts in 6h; consider quarantine"))

    oldest = await session.scalar(
        select(func.min(Job.created_at)).where(
            Job.desired_state == "run",
            Job.accepted_attempt_id.is_(None),
            func.coalesce(Job.pending_reason, "") != "building environment",
        )
    )
    if oldest is not None and now - oldest > timedelta(seconds=settings.alert_queue_age_seconds):
        hours = (now - oldest).total_seconds() / 3600
        alerts.append(Alert("queue_age", "warning", f"oldest queued job has waited {hours:.1f} h"))
    return alerts


# --- Lifecycle ---------------------------------------------------------------


async def sync_alerts(session: AsyncSession, current: list[Alert], now: datetime) -> None:
    """Bring the table in line with what `check()` found: new keys are opened,
    keys that are gone are resolved, resolved keys that return start a new
    episode (acknowledgement and notification state reset; a mute stays)."""
    await session.execute(select(func.pg_advisory_xact_lock(_ALERT_LOCK)))
    wanted = {a.key: a for a in current}
    rows = {
        row.key: row
        for row in (
            await session.scalars(
                select(AlertRecord).where(or_(AlertRecord.key.in_(wanted), AlertRecord.resolved_at.is_(None)))
            )
        ).all()
    }
    for key, alert in wanted.items():
        row = rows.get(key)
        if row is None:
            session.add(
                AlertRecord(
                    key=key,
                    kind=alert.kind,
                    severity=alert.severity,
                    message=alert.message,
                    first_seen=now,
                    last_seen=now,
                )
            )
            continue
        if row.resolved_at is not None:
            row.resolved_at = None
            row.first_seen = now
            row.acknowledged_at = None
            row.acknowledged_by = None
            row.notified_at = None
        row.severity = alert.severity
        row.message = alert.message
        row.last_seen = now
    for key, row in rows.items():
        if key not in wanted and row.resolved_at is None:
            row.resolved_at = now
    await session.flush()


async def resolve_node_alerts(session: AsyncSession, node_id: uuid.UUID, now: datetime) -> None:
    """A forgotten PC's alerts end with it instead of waiting for the next check."""
    await session.execute(
        update(AlertRecord)
        .where(
            AlertRecord.key.in_((f"offline:{node_id}", f"disk:{node_id}")),
            AlertRecord.resolved_at.is_(None),
        )
        .values(resolved_at=now)
    )


def is_muted(row: AlertRecord, now: datetime) -> bool:
    return row.muted_until is not None and row.muted_until > now


def alert_status(row: AlertRecord, now: datetime) -> str:
    if row.resolved_at is not None:
        return "resolved"
    if row.acknowledged_at is not None:
        return "acknowledged"
    return "muted" if is_muted(row, now) else "open"


def serialize_alert(row: AlertRecord, now: datetime) -> dict:
    node_id = None
    if row.kind in {"offline", "disk"}:
        try:
            node_id = str(uuid.UUID(row.key.split(":", 1)[1]))
        except (IndexError, ValueError):
            node_id = None
    return {
        "id": str(row.id),
        "key": row.key,
        "kind": row.kind,
        "severity": row.severity,
        "message": row.message,
        "status": alert_status(row, now),
        "node_id": node_id,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
        "acknowledged_by": row.acknowledged_by,
        "muted_until": row.muted_until.isoformat() if row.muted_until else None,
    }


async def grouped_alerts(session: AsyncSession, now: datetime, include_resolved: bool = False) -> list[dict]:
    """Open alerts (and, on request, those resolved in the last day) by kind,
    worst group first."""
    visible = AlertRecord.resolved_at.is_(None)
    if include_resolved:
        visible = or_(visible, AlertRecord.resolved_at >= now - RESOLVED_VISIBLE)
    rows = (await session.scalars(select(AlertRecord).where(visible))).all()

    def rank(row: AlertRecord) -> int:
        return SEVERITY_RANK.get(row.severity, 0)

    groups: dict[str, list[AlertRecord]] = {}
    for row in rows:
        groups.setdefault(row.kind, []).append(row)
    out = []
    for kind, members in groups.items():
        members.sort(key=lambda r: (r.resolved_at is not None, -rank(r), r.first_seen))
        open_rows = [r for r in members if r.resolved_at is None]
        pool = open_rows or members
        out.append(
            {
                "kind": kind,
                "count": len(open_rows),
                "unhandled": sum(1 for r in open_rows if alert_status(r, now) == "open"),
                "severity": max((r.severity for r in pool), key=lambda s: SEVERITY_RANK.get(s, 0)),
                "alerts": [serialize_alert(r, now) for r in members],
            }
        )
    out.sort(key=lambda g: (-SEVERITY_RANK.get(g["severity"], 0), g["count"] == 0, g["kind"]))
    return out


async def get_alert(session: AsyncSession, alert_id: uuid.UUID) -> AlertRecord:
    row = await session.get(AlertRecord, alert_id)
    if row is None:
        raise AppError(404, "not_found", "alert not found")
    return row


# --- Notifier ----------------------------------------------------------------

Poster = Callable[[str, dict], Awaitable[None]]


async def _post_webhook(url: str, payload: dict) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(url, json=payload)


class Notifier:
    """Posts alerts that are open, not acknowledged, not muted and not yet
    announced (or last announced more than ALERT_COOLDOWN_SECONDS ago)."""

    def __init__(self, post: Poster | None = None) -> None:
        self.post = post or _post_webhook

    async def send_new(self, session: AsyncSession, now: datetime) -> list[AlertRecord]:
        if not settings.alert_webhook_url:
            return []
        cooldown = timedelta(seconds=settings.alert_cooldown_seconds)
        due = (
            await session.scalars(
                select(AlertRecord)
                .where(
                    AlertRecord.resolved_at.is_(None),
                    AlertRecord.acknowledged_at.is_(None),
                    or_(AlertRecord.muted_until.is_(None), AlertRecord.muted_until <= now),
                    or_(AlertRecord.notified_at.is_(None), AlertRecord.notified_at <= now - cooldown),
                )
                .order_by(AlertRecord.first_seen)
            )
        ).all()
        if not due:
            return []
        text = "\n".join(f"[{a.severity}] {a.message}" for a in due)
        try:
            await self.post(settings.alert_webhook_url, {"text": "Lab platform alerts:\n" + text})
        except httpx.HTTPError:
            return []
        for row in due:
            row.notified_at = now
        await session.flush()
        return list(due)


async def run_cycle(session: AsyncSession, notifier: Notifier) -> list[AlertRecord]:
    """One pass of the alert worker: check, update the table, announce, commit."""
    now = await db_now(session)
    await sync_alerts(session, await check(session), now)
    sent = await notifier.send_new(session, now)
    await session.commit()
    return sent
