"""Prometheus metrics (B10).

Gauges describe the state of the lab right now and are filled in from the
database when Prometheus scrapes. Counters and histograms are bumped where the
event happens; `observe_after_commit` holds them back until the surrounding
transaction commits, so a rolled-back request never counts.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.enums import ACTIVE_ATTEMPT_STATES
from app.models import EnvironmentBuild, Job, JobAttempt, Node

REGISTRY = CollectorRegistry()

OUTCOMES = ("completed", "failed", "cancelled")
CLAIM_RESULTS = ("job", "build", "empty")
NODE_STATES = ("idle", "busy", "offline", "draining", "quarantined")
BUILD_STATES = ("pending", "building", "testing", "ready", "failed")

QUEUE_JOBS = Gauge("lab_queue_jobs", "Jobs waiting for a node, by requested GPU class.", ["gpu_class"], registry=REGISTRY)
NODES = Gauge("lab_nodes", "Lab PCs by state.", ["state"], registry=REGISTRY)
RUNNING_ATTEMPTS = Gauge(
    "lab_running_attempts", "Attempts holding a node (leased through uploading).", registry=REGISTRY
)
BUILDS = Gauge("lab_builds", "Environment builds by state.", ["state"], registry=REGISTRY)

JOBS_FINISHED = Counter(
    "lab_jobs_finished", "Jobs that reached a final outcome.", ["outcome"], registry=REGISTRY
)
LEASE_LOST = Counter("lab_lease_lost", "Attempts failed because their node stopped renewing.", registry=REGISTRY)
CLAIMS = Counter("lab_claims", "Claim requests by result.", ["result"], registry=REGISTRY)
CLIENT_ERRORS = Counter("lab_client_errors", "Errors the portal reported from users' browsers.", registry=REGISTRY)

_START_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200, 21600, 86400)
_PULL_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800)
_RUNTIME_BUCKETS = (10, 30, 60, 300, 900, 1800, 3600, 7200, 14400, 28800, 86400)
TIME_TO_START = Histogram(
    "lab_time_to_start_seconds",
    "Seconds from submission to the first attempt being created.",
    buckets=_START_BUCKETS,
    registry=REGISTRY,
)
IMAGE_PULL = Histogram(
    "lab_image_pull_seconds", "Seconds a node spent pulling a job image.", buckets=_PULL_BUCKETS, registry=REGISTRY
)
JOB_RUNTIME = Histogram(
    "lab_job_runtime_seconds",
    "Seconds from start to the end of an attempt.",
    buckets=_RUNTIME_BUCKETS,
    registry=REGISTRY,
)

# Children exist from the start so every series shows up (as 0) before its
# first event; rate() and the dashboard need that.
for _outcome in OUTCOMES:
    JOBS_FINISHED.labels(outcome=_outcome)
for _result in CLAIM_RESULTS:
    CLAIMS.labels(result=_result)

_PENDING_KEY = "metrics_after_commit"


def observe_after_commit(session: AsyncSession, action: Callable[[], None]) -> None:
    session.sync_session.info.setdefault(_PENDING_KEY, []).append(action)


@event.listens_for(Session, "after_commit")
def _run_pending(session: Session) -> None:
    for action in session.info.pop(_PENDING_KEY, []):
        try:
            action()
        except Exception:  # metrics must never break a request
            pass


@event.listens_for(Session, "after_rollback")
def _drop_pending(session: Session) -> None:
    session.info.pop(_PENDING_KEY, None)


def job_finished(session: AsyncSession, outcome: str) -> None:
    observe_after_commit(session, lambda: JOBS_FINISHED.labels(outcome=outcome).inc())


def lease_lost(session: AsyncSession) -> None:
    observe_after_commit(session, LEASE_LOST.inc)


def attempt_ended(session: AsyncSession, started_at: datetime | None, now: datetime) -> None:
    if started_at is not None:
        seconds = max(0.0, (now - started_at).total_seconds())
        observe_after_commit(session, lambda: JOB_RUNTIME.observe(seconds))


def attempt_created(session: AsyncSession, job_created_at: datetime | None, attempt_number: int, now: datetime) -> None:
    """Time to start counts the first attempt only; a retry's wait is not the
    submitter's wait."""
    if job_created_at is not None and attempt_number == 1:
        seconds = max(0.0, (now - job_created_at).total_seconds())
        observe_after_commit(session, lambda: TIME_TO_START.observe(seconds))


def image_pulled(session: AsyncSession, seconds: float) -> None:
    observe_after_commit(session, lambda: IMAGE_PULL.observe(max(0.0, seconds)))


def claimed(result: str) -> None:
    CLAIMS.labels(result=result).inc()


# --- Gauges, computed at scrape time -------------------------------------------


def node_state(node: Node, busy_ids: set, now: datetime) -> str:
    from app.scheduler import _heartbeat_fresh

    if node.eligibility == "quarantined":
        return "quarantined"
    if node.eligibility == "draining":
        return "draining"
    if node.eligibility == "offline" or not _heartbeat_fresh(node, now):
        return "offline"
    return "busy" if node.id in busy_ids else "idle"


def _active_nodes_stmt():
    return select(Node).where(Node.decommissioned_at.is_(None))


async def refresh_gauges(session: AsyncSession) -> None:
    from app.scheduler import db_now

    now = await db_now(session)

    QUEUE_JOBS.clear()
    active = (
        select(JobAttempt.id)
        .where(JobAttempt.job_id == Job.id, JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
        .exists()
    )
    gpu_class = func.coalesce(Job.resources["gpu_class"].astext, "any").label("gpu_class")
    queued = await session.execute(
        select(gpu_class, func.count())
        .where(
            Job.desired_state == "run",
            Job.accepted_attempt_id.is_(None),
            func.coalesce(Job.pending_reason, "") != "building environment",
            ~active,
        )
        .group_by(gpu_class)
    )
    for name, count in queued.all():
        QUEUE_JOBS.labels(gpu_class=name or "any").set(count)

    busy_ids = set(
        (
            await session.scalars(
                select(JobAttempt.node_id).where(JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
            )
        ).all()
    )
    counts = dict.fromkeys(NODE_STATES, 0)
    for node in (await session.scalars(_active_nodes_stmt())).all():
        counts[node_state(node, busy_ids, now)] += 1
    for state, count in counts.items():
        NODES.labels(state=state).set(count)

    RUNNING_ATTEMPTS.set(
        await session.scalar(
            select(func.count()).select_from(JobAttempt).where(JobAttempt.observed_state.in_(ACTIVE_ATTEMPT_STATES))
        )
        or 0
    )

    build_counts = dict.fromkeys(BUILD_STATES, 0)
    for state, count in (
        await session.execute(select(EnvironmentBuild.state, func.count()).group_by(EnvironmentBuild.state))
    ).all():
        build_counts[state] = count
    for state, count in build_counts.items():
        BUILDS.labels(state=state).set(count)


_scrape_lock = asyncio.Lock()


async def render(session: AsyncSession) -> bytes:
    # The gauges are shared state; two scrapes at once would interleave clear() and set().
    async with _scrape_lock:
        await refresh_gauges(session)
        return generate_latest(REGISTRY)

