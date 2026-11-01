"""Operational alerts: the conditions an admin must hear about without watching
the portal. `check()` builds the current list; the alert worker posts new ones
to ALERT_WEBHOOK_URL (Slack/Teams/Discord-style JSON) with a cooldown."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Job, JobAttempt, Node
from app.scheduler import db_now, is_train_node


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    message: str


async def check(session: AsyncSession) -> list[Alert]:
    now = await db_now(session)
    alerts: list[Alert] = []

    nodes = (await session.scalars(select(Node))).all()
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


class Notifier:
    def __init__(self) -> None:
        self.last_sent: dict[str, float] = {}

    async def send_new(self, alerts: list[Alert]) -> list[Alert]:
        due = [a for a in alerts if time.time() - self.last_sent.get(a.key, 0) > settings.alert_cooldown_seconds]
        if not due or not settings.alert_webhook_url:
            return []
        text = "\n".join(f"[{a.severity}] {a.message}" for a in due)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(settings.alert_webhook_url, json={"text": "Lab platform alerts:\n" + text})
        except httpx.HTTPError:
            return []
        for alert in due:
            self.last_sent[alert.key] = time.time()
        return due
