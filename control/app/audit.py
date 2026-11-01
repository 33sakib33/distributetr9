"""Audit log (B21): one row per admin mutation, written in the same
transaction as the change so the log never disagrees with the data."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

# Every action name record_audit is called with (the portal's filter list).
ACTIONS = (
    "node.drain",
    "node.quarantine",
    "node.release",
    "node.revoke",
    "node.forget",
    "node.ring",
    "node.meta",
    "node.logs_request",
    "job.requeue",
    "job.bulk_cancel",
    "job.cancel",
    "alert.ack",
    "alert.mute",
    "alert.ack_kind",
    "schedule.create",
    "schedule.update",
    "schedule.delete",
    "quota.set",
    "user.block",
    "user.unblock",
    "member.add",
    "member.role",
    "member.remove",
    "environment.create",
    "environment.update",
    "environment.retire",
    "environment.restore",
    "rollout.update",
)


def record_audit(
    session: AsyncSession,
    actor: User,
    action: str,
    target_type: str,
    target_id: object | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Add the row to `session`; the caller's commit saves it with the change."""
    session.add(
        AuditLog(
            actor_user_id=actor.id,
            actor_email=actor.email,
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            details=details or None,
        )
    )
