"""What a student can expect before and while a job waits (B23, B24).

`GET /availability` lists, per GPU class, how many PCs there are, how many are
reachable, how many could take a job right now, how many jobs wait and a rough
wait. A queued job gets its place in line and an estimated start from the same
numbers.

Estimates are deliberately crude: the median runtime of the last 7 days
stands in for how long a PC stays busy.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import platform_settings
from app.config import settings
from app.enums import ACTIVE_ATTEMPT_STATES
from app.models import Job, JobAttempt, Node
from app.schedule import Schedule, load_schedule
from app.scheduler import db_now, is_online, is_train_node, node_can_claim

DEFAULT_RUNTIME_SECONDS = 600
MAX_WAIT_SECONDS = 24 * 3600
RUNTIME_WINDOW = timedelta(days=7)


def estimate_wait_seconds(*, queued: int, online: int, free: int, median: int) -> int | None:
    """0 with a free PC; None when no PC of the class is reachable; otherwise
    the queue (and this job) shared out over the reachable PCs, in medians."""
    if free > 0:
        return 0
    if online <= 0:
        return None
    rounds = math.ceil((queued + 1) / max(online, 1))
    return min(MAX_WAIT_SECONDS, rounds * median)


def estimate_start_seconds(*, position: int, online: int, free: int, median: int) -> int | None:
    """The same estimate for the job `position` places from the front: the
    jobs ahead of it take the free PCs first, the rest wait like it does."""
    ahead = max(0, position - 1)
    if free > ahead:
        return 0
    return estimate_wait_seconds(queued=ahead - free, online=online, free=0, median=median)


def is_reachable(node: Node, now: datetime) -> bool:
    """Heartbeating and taking work: drained and quarantined PCs do not count."""
    return is_online(node, now) and node.eligibility == "eligible"


def in_class(node: Node, gpu_class: str | None) -> bool:
    if not gpu_class:
        return True
    return node.compatibility_class == gpu_class or (not settings.strict_node_class and node.gpu_model == gpu_class)


def class_counts(
    nodes: list[Node], busy_ids: set, now: datetime, schedule: Schedule | None
) -> tuple[int, int, int]:
    """(total, online, free) for these nodes."""
    online = [n for n in nodes if is_reachable(n, now)]
    free = [n for n in online if n.id not in busy_ids and node_can_claim(n, now, schedule) is None]
    return len(nodes), len(online), len(free)


async def median_runtime_seconds(session: AsyncSession, now: datetime) -> int:
    """Median run time of attempts that completed in the last 7 days."""
    seconds = func.extract("epoch", JobAttempt.completed_at - JobAttempt.started_at)
    median = await session.scalar(
        select(func.percentile_cont(0.5).within_group(seconds)).where(
            JobAttempt.observed_state == "completed",
            JobAttempt.started_at.is_not(None),
            JobAttempt.completed_at >= now - RUNTIME_WINDOW,
        )
    )
    return max(1, int(round(median))) if median else DEFAULT_RUNTIME_SECONDS


def _runnable():
    """Jobs that wait for a PC: asked to run, not finished, no attempt in flight,
    not held back by an environment build."""
    active = (
        select(JobAttempt.id)
        .where(JobAttempt.job_id == Job.id, JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
        .exists()
    )
    return (
        Job.desired_state == "run",
        Job.accepted_attempt_id.is_(None),
        func.coalesce(Job.pending_reason, "") != "building environment",
        ~active,
    )


async def _fleet(session: AsyncSession) -> tuple[list[Node], set, Schedule]:
    await platform_settings.ensure_loaded(session)
    nodes = list(
        (await session.scalars(select(Node).where(Node.decommissioned_at.is_(None)).order_by(Node.hostname))).all()
    )
    nodes = [n for n in nodes if is_train_node(n)]
    busy = set(
        (
            await session.scalars(
                select(JobAttempt.node_id).where(JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
            )
        ).all()
    )
    return nodes, busy, await load_schedule(session)


def _recent(node: Node, now: datetime) -> bool:
    """Heard from within AVAILABILITY_CLASS_MAX_AGE_DAYS: older PCs are gone, not a class."""
    beat = node.last_heartbeat_at
    if beat is None:
        return False
    if beat.tzinfo is None:
        beat = beat.replace(tzinfo=now.tzinfo)
    return beat >= now - timedelta(days=settings.availability_class_max_age_days)


def _top_model(members: list[Node]) -> str | None:
    """The most common GPU model among the nodes (ties go to the first by name)."""
    counts = Counter(n.gpu_model for n in members if n.gpu_model)
    if not counts:
        return None
    best = max(counts.values())
    return sorted(m for m, c in counts.items() if c == best)[0]


async def availability(session: AsyncSession) -> list[dict]:
    now = await db_now(session)
    nodes, busy, schedule = await _fleet(session)
    nodes = [n for n in nodes if _recent(n, now)]
    median = await median_runtime_seconds(session, now)
    gpu_class = Job.resources["gpu_class"].astext
    queued = dict(
        (
            await session.execute(
                select(gpu_class, func.count()).where(*_runnable()).group_by(gpu_class)
            )
        ).all()
    )
    by_class: dict[str, list[Node]] = {}
    for node in nodes:
        by_class.setdefault(node.compatibility_class, []).append(node)
    # CPU-only jobs run on any train node, so "cpu" is always offered and counts them all.
    by_class["cpu"] = nodes
    out = []
    for name, members in sorted(by_class.items()):
        total, online, free = class_counts(members, busy, now, schedule)
        waiting = int(queued.get(name, 0))
        if name == "cpu":
            waiting += int(queued.get(None, 0))
        out.append(
            {
                "gpu_class": name,
                "gpu_model": None if name == "cpu" else _top_model(members),
                "vram_mb": 0 if name == "cpu" else max(n.vram_allocatable_mb for n in members),
                "nodes_total": total,
                "nodes_online": online,
                "nodes_free": free,
                "queued": waiting,
                "est_wait_seconds": estimate_wait_seconds(queued=waiting, online=online, free=free, median=median),
            }
        )
    return out


async def queue_position(session: AsyncSession, job: Job) -> int:
    """1-based place among the runnable jobs of the same GPU class: higher
    priority first, then older first."""
    gpu_class = Job.resources["gpu_class"].astext
    wanted = (job.resources or {}).get("gpu_class")
    ahead = await session.scalar(
        select(func.count()).where(
            *_runnable(),
            gpu_class == wanted if wanted else gpu_class.is_(None),
            or_(
                Job.priority > job.priority,
                and_(
                    Job.priority == job.priority,
                    or_(Job.created_at < job.created_at, and_(Job.created_at == job.created_at, Job.id < job.id)),
                ),
            ),
        )
    )
    return int(ahead or 0) + 1


async def queue_info(session: AsyncSession, job: Job) -> tuple[int, int | None]:
    """(position, estimated seconds until it starts) for a queued job."""
    position = await queue_position(session, job)
    now = await db_now(session)
    nodes, busy, schedule = await _fleet(session)
    wanted = (job.resources or {}).get("gpu_class")
    _total, online, free = class_counts([n for n in nodes if in_class(n, wanted)], busy, now, schedule)
    median = await median_runtime_seconds(session, now)
    return position, estimate_start_seconds(position=position, online=online, free=free, median=median)
