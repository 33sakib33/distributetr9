from __future__ import annotations

from datetime import datetime

from app.enums import ACTIVE_ATTEMPT_STATES, TERMINAL_ATTEMPT_STATES
from app.models import Job, JobAttempt


def latest_attempt(job: Job) -> JobAttempt | None:
    if not job.attempts:
        return None
    return max(job.attempts, key=lambda a: a.attempt_number)


def derived_status(job: Job) -> str:
    attempt = latest_attempt(job)
    accepted = None
    if job.accepted_attempt_id:
        accepted = next((a for a in job.attempts if a.id == job.accepted_attempt_id), None)

    if accepted and accepted.observed_state == "completed":
        return "completed"
    if job.desired_state == "cancel":
        if attempt is None or attempt.observed_state in {"cancelled", "failed"}:
            return "cancelled"
        if attempt.observed_state != "completed":
            return "cancelling"
    if accepted and accepted.observed_state == "cancelled":
        return "cancelled"
    if accepted and accepted.observed_state == "failed":
        return "failed"
    if attempt is None:
        reason = job.pending_reason or ""
        if job.completed_at and reason == "environment build failed":
            return "failed"
        if reason == "building environment":
            return "building"
        return "queued"
    if attempt.observed_state == "failed":
        if job.desired_state == "run" and attempt.attempt_number < job.max_attempts:
            return "queued"
        return "failed"
    if attempt.observed_state == "cancelled":
        # An admin requeue turns a cancelled job back to run with its old attempt in place.
        return "cancelled" if job.desired_state == "cancel" else "queued"
    if attempt.observed_state == "completed":
        return "completed"
    if attempt.observed_state == "unknown":
        return "unknown"
    if attempt.observed_state in {"leased", "starting"}:
        return "starting"
    if attempt.observed_state == "running":
        return "running"
    if attempt.observed_state == "uploading":
        return "uploading"
    if attempt.observed_state == "cancelling":
        return "cancelling"
    return attempt.observed_state


def _ms(start: datetime | None, end: datetime | None) -> int:
    if not start or not end:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def timings(job: Job, now: datetime) -> dict[str, int]:
    attempt = latest_attempt(job)
    if attempt is None:
        return {
            "queued_ms": _ms(job.created_at, now),
            "starting_ms": 0,
            "running_ms": 0,
            "uploading_ms": 0,
            "building_ms": 0,
        }
    start = attempt.started_at or attempt.created_at
    running_end = attempt.completed_at or now
    return {
        "queued_ms": _ms(job.created_at, attempt.created_at),
        "starting_ms": _ms(attempt.created_at, start),
        "running_ms": _ms(start, running_end),
        "uploading_ms": 0,
        "building_ms": 0,
    }


def is_active_state(state: str) -> bool:
    return state in ACTIVE_ATTEMPT_STATES


def is_terminal_state(state: str) -> bool:
    return state in TERMINAL_ATTEMPT_STATES
