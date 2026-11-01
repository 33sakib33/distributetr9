"""Usage reports (B12): GPU-hours and job counts per user, project or GPU
class, and how busy the lab was.

Everything is counted from attempts that overlap the requested window, with
each attempt's run time clipped to the window, so a job that straddles the
boundary is split fairly. GPU-hours only count jobs that asked for a GPU
(`min_vram_mb > 0`). Online time comes from node health samples, one per
minute per PC.
"""
from __future__ import annotations

import csv
import io
import uuid
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import DateTime, Float, case, cast, extract, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.errors import AppError
from app.models import Job, JobAttempt, Node, NodeHealth, Project, User

DEFAULT_DAYS = 30
GROUPS = ("user", "project", "class")
CSV_COLUMNS = ("key", "label", "gpu_hours", "jobs", "succeeded", "failed")
# A health sample is taken at most once a minute (see telemetry.record_health).
SAMPLE_MINUTES = 1


def parse_moment(raw: str, *, end: bool = False) -> datetime:
    """ISO date or datetime. A bare date is midnight UTC; as the end of a range
    it covers that whole day."""
    text = raw.strip()
    try:
        if len(text) == 10:
            moment = datetime.combine(date.fromisoformat(text), time.min, tzinfo=timezone.utc)
            return moment + timedelta(days=1) if end else moment
        moment = datetime.fromisoformat(text)
    except ValueError:
        raise AppError(400, "invalid", f"not a date or ISO timestamp: {raw}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def window(start: str | None, end: str | None, now: datetime) -> tuple[datetime, datetime]:
    stop = parse_moment(end, end=True) if end else now
    begin = parse_moment(start) if start else stop - timedelta(days=DEFAULT_DAYS)
    if begin >= stop:
        raise AppError(400, "invalid", "'from' must be before 'to'")
    return begin, stop


def _seconds(begin: datetime, stop: datetime, now: datetime):
    """Seconds an attempt ran inside the window (0 for one that never started)."""
    lo = func.greatest(JobAttempt.started_at, literal(begin, DateTime(timezone=True)))
    hi = func.least(
        func.coalesce(JobAttempt.completed_at, literal(now, DateTime(timezone=True))),
        literal(stop, DateTime(timezone=True)),
    )
    return func.greatest(extract("epoch", hi - lo), 0)


def _overlapping(begin: datetime, stop: datetime, now: datetime):
    return (
        JobAttempt.started_at.is_not(None),
        JobAttempt.started_at < stop,
        func.coalesce(JobAttempt.completed_at, now) > begin,
    )


def _hours(value: float | None) -> float:
    return round(float(value or 0) / 3600.0, 4)


async def usage_rows(
    session: AsyncSession,
    *,
    group: str,
    begin: datetime,
    stop: datetime,
    now: datetime,
    user_id: uuid.UUID | None = None,
) -> list[dict]:
    if group not in GROUPS:
        raise AppError(400, "invalid", f"group must be one of {', '.join(GROUPS)}")
    seconds = _seconds(begin, stop, now)
    min_vram = func.coalesce(cast(Job.resources["min_vram_mb"].astext, Float), 0)
    accepted = aliased(JobAttempt)

    if group == "user":
        key, label = User.id, User.email
    elif group == "project":
        key, label = Project.id, Project.name
    else:
        key, label = Node.compatibility_class, Node.compatibility_class

    stmt = (
        select(
            key.label("key"),
            label.label("label"),
            func.coalesce(func.sum(case((min_vram > 0, seconds), else_=0)), 0).label("gpu_seconds"),
            func.count(Job.id.distinct()).label("jobs"),
            func.count(Job.id.distinct()).filter(accepted.observed_state == "completed").label("succeeded"),
            func.count(Job.id.distinct()).filter(accepted.observed_state == "failed").label("failed"),
        )
        .select_from(JobAttempt)
        .join(Job, Job.id == JobAttempt.job_id)
        .outerjoin(accepted, accepted.id == Job.accepted_attempt_id)
        .where(*_overlapping(begin, stop, now))
        .group_by(key, label)
    )
    if group == "user":
        stmt = stmt.join(User, User.id == Job.user_id)
    elif group == "project":
        stmt = stmt.join(Project, Project.id == Job.project_id)
    else:
        stmt = stmt.join(Node, Node.id == JobAttempt.node_id)
    if user_id is not None:
        stmt = stmt.where(Job.user_id == user_id)

    rows = [
        {
            "key": str(r.key),
            "label": str(r.label),
            "gpu_hours": _hours(r.gpu_seconds),
            "jobs": int(r.jobs),
            "succeeded": int(r.succeeded),
            "failed": int(r.failed),
        }
        for r in (await session.execute(stmt)).all()
    ]
    rows.sort(key=lambda r: (-r["gpu_hours"], -r["jobs"], r["label"]))
    return rows


async def lab_usage(session: AsyncSession, *, begin: datetime, stop: datetime, now: datetime) -> dict:
    busy_seconds = await session.scalar(
        select(func.coalesce(func.sum(_seconds(begin, stop, now)), 0)).where(*_overlapping(begin, stop, now))
    )
    samples = await session.scalar(
        select(func.count())
        .select_from(NodeHealth)
        .join(Node, Node.id == NodeHealth.node_id)
        .where(NodeHealth.at >= begin, NodeHealth.at < stop, Node.role == "train")
    )
    online = round((samples or 0) * SAMPLE_MINUTES / 60.0, 4)
    busy = _hours(busy_seconds)
    return {
        "online_hours": online,
        "busy_hours": busy,
        "utilisation": round(min(1.0, busy / online), 4) if online > 0 else 0.0,
    }


def _safe_cell(value: object) -> object:
    """Keep a spreadsheet from running a project name as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def to_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_safe_cell(row[column]) if column in {"key", "label"} else row[column] for column in CSV_COLUMNS])
    return out.getvalue()
