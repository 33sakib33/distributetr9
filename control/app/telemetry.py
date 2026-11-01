"""Job usage samples, job stages and node health history (B6-B9).

Everything the agent reports here is advisory: a bad value is dropped, never an
error, so telemetry can not break lease renewal.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import metrics
from app.enums import TERMINAL_ATTEMPT_STATES
from app.models import AttemptUsage, Environment, EnvironmentBuild, Job, JobAttempt, Node, NodeHealth
from app.schemas import HealthIn, StageIn, UsageIn

MAX_FINISHED_SAMPLES = 300
HEALTH_MIN_INTERVAL = timedelta(seconds=60)
HEALTH_KEEP = timedelta(days=7)
# Stages the agent may report; building_environment is derived from the build row.
AGENT_STAGES = {"pulling_image", "running", "uploading"}


def _num(value: float | None) -> float | None:
    return None if value is None else float(value)


def _mb(value: float | None) -> int | None:
    return None if value is None else max(0, int(round(value)))


# --- Usage -------------------------------------------------------------------


async def record_usage(
    session: AsyncSession, attempt: JobAttempt, usage: UsageIn, now: datetime
) -> bool:
    """Store one sample and fold it into the attempt's peaks. A sample with no
    values at all (no GPU, no docker) is not worth a row."""
    gpu, cpu = _num(usage.gpu_pct), _num(usage.cpu_pct)
    vram, ram = _mb(usage.vram_mb), _mb(usage.ram_mb)
    if gpu is None and cpu is None and vram is None and ram is None:
        return False
    session.add(
        AttemptUsage(attempt_id=attempt.id, at=now, gpu_pct=gpu, vram_mb=vram, cpu_pct=cpu, ram_mb=ram)
    )
    if vram is not None:
        attempt.peak_vram_mb = max(attempt.peak_vram_mb or 0, vram)
    if ram is not None:
        attempt.peak_ram_mb = max(attempt.peak_ram_mb or 0, ram)
    if gpu is not None:
        await session.flush()
        attempt.avg_gpu_pct = await session.scalar(
            select(func.avg(AttemptUsage.gpu_pct)).where(AttemptUsage.attempt_id == attempt.id)
        )
    return True


def indices_to_keep(count: int, keep: int) -> list[int]:
    """`keep` evenly spaced positions out of `count`, always including the first
    and the last."""
    if count <= keep:
        return list(range(count))
    if keep <= 1:
        return [count - 1]
    return sorted({round(i * (count - 1) / (keep - 1)) for i in range(keep)})


async def thin_usage(session: AsyncSession, keep: int = MAX_FINISHED_SAMPLES) -> int:
    """Cut each finished attempt down to at most `keep` evenly spaced samples.
    Running attempts are left alone. Returns how many rows were deleted."""
    crowded = (
        await session.execute(
            select(AttemptUsage.attempt_id)
            .join(JobAttempt, JobAttempt.id == AttemptUsage.attempt_id)
            .where(JobAttempt.observed_state.in_(TERMINAL_ATTEMPT_STATES))
            .group_by(AttemptUsage.attempt_id)
            .having(func.count() > keep)
        )
    ).scalars().all()
    deleted = 0
    for attempt_id in crowded:
        ids = (
            await session.scalars(
                select(AttemptUsage.id)
                .where(AttemptUsage.attempt_id == attempt_id)
                .order_by(AttemptUsage.at, AttemptUsage.id)
            )
        ).all()
        kept = set(indices_to_keep(len(ids), keep))
        drop = [row_id for i, row_id in enumerate(ids) if i not in kept]
        for start in range(0, len(drop), 1000):
            await session.execute(delete(AttemptUsage).where(AttemptUsage.id.in_(drop[start : start + 1000])))
        deleted += len(drop)
    return deleted


async def usage_for_attempt(session: AsyncSession, attempt: JobAttempt, node: Node | None) -> dict:
    rows = (
        await session.scalars(
            select(AttemptUsage).where(AttemptUsage.attempt_id == attempt.id).order_by(AttemptUsage.at)
        )
    ).all()
    return {
        "attempt_id": str(attempt.id),
        "samples": [
            {
                "at": r.at.isoformat(),
                "gpu_pct": r.gpu_pct,
                "vram_mb": r.vram_mb,
                "cpu_pct": r.cpu_pct,
                "ram_mb": r.ram_mb,
            }
            for r in rows
        ],
        "peak_vram_mb": attempt.peak_vram_mb,
        "peak_ram_mb": attempt.peak_ram_mb,
        "avg_gpu_pct": attempt.avg_gpu_pct,
        "vram_total_mb": node.vram_total_mb if node is not None else None,
    }


# --- Stages ------------------------------------------------------------------


def _valid_progress(progress: dict[str, Any] | None) -> dict | None:
    if not isinstance(progress, dict):
        return None
    try:
        return {
            "done": int(progress["done"]),
            "total": int(progress["total"]),
            "unit": str(progress.get("unit") or ""),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _seconds_between(start: str | None, end: datetime) -> float | None:
    try:
        return (end - datetime.fromisoformat(start)).total_seconds() if start else None
    except (TypeError, ValueError):
        return None


def apply_stage(attempt: JobAttempt, event: StageIn, now: datetime, session: AsyncSession | None = None) -> None:
    """Fold one agent stage event into the attempt's stage map. A pull that
    finishes here is recorded in the image pull histogram."""
    if event.name not in AGENT_STAGES:
        return
    stages = dict(attempt.stages or {})
    entry = dict(stages.get(event.name) or {"started_at": None, "finished_at": None, "progress": None})
    stamp = now.isoformat()
    entry["started_at"] = entry["started_at"] or stamp
    if event.state == "finished":
        if event.name == "pulling_image" and not entry["finished_at"] and session is not None:
            pulled = _seconds_between(entry["started_at"], now)
            if pulled is not None:
                metrics.image_pulled(session, pulled)
        entry["finished_at"] = entry["finished_at"] or stamp
    progress = _valid_progress(event.progress)
    if progress is not None:
        entry["progress"] = progress
    stages[event.name] = entry
    attempt.stages = stages


def apply_stages(
    attempt: JobAttempt, events: list[StageIn], now: datetime, session: AsyncSession | None = None
) -> None:
    for event in events:
        apply_stage(attempt, event, now, session)


def enter_uploading(attempt: JobAttempt, now: datetime) -> None:
    """The attempt moved to uploading: whatever was running is over."""
    stamp = now.isoformat()
    stages = dict(attempt.stages or {})
    for name in ("pulling_image", "running"):
        if name in stages and not stages[name].get("finished_at"):
            stages[name] = {**stages[name], "finished_at": stamp}
    stages.setdefault("uploading", {"started_at": stamp, "finished_at": None, "progress": None})
    attempt.stages = stages


def stages_for(attempt: JobAttempt | None, build: EnvironmentBuild | None = None) -> dict:
    """The stage map as the API shows it: open stages of a finished attempt end
    when the attempt did, and a pip build adds `building_environment`."""
    stages: dict[str, dict] = {}
    if build is not None and build.kind == "pip":
        started = build.started_at or build.created_at
        stages["building_environment"] = {
            "started_at": started.isoformat() if started else None,
            "finished_at": build.finished_at.isoformat() if build.finished_at else None,
            "progress": None,
        }
    if attempt is None:
        return stages
    over = attempt.observed_state in TERMINAL_ATTEMPT_STATES and attempt.completed_at
    for name, entry in (attempt.stages or {}).items():
        shown = dict(entry)
        if over and not shown.get("finished_at"):
            shown["finished_at"] = attempt.completed_at.isoformat()
        stages[name] = shown
    return stages


def building_environment(build: EnvironmentBuild | None) -> dict | None:
    """Set only while a custom environment is waiting for or in a build."""
    if build is None or build.kind != "pip" or build.state not in {"pending", "building", "testing"}:
        return None
    started = build.started_at
    return {"state": build.state, "started_at": started.isoformat() if started else None}


async def build_for_job(session: AsyncSession, job: Job) -> EnvironmentBuild | None:
    env = await session.get(Environment, job.environment_id)
    if env is None or not str(env.name).startswith("build-"):
        return None
    return await session.scalar(
        select(EnvironmentBuild).where(
            EnvironmentBuild.environment_hash == env.name.removeprefix("build-")
        )
    )


# --- Node health -------------------------------------------------------------


async def record_health(
    session: AsyncSession,
    node: Node,
    health: HealthIn | None,
    *,
    disk_free_bytes: int,
    busy: bool,
    now: datetime,
) -> bool:
    """Keep at most one health row per node per minute. Agents that predate the
    `health` field still leave a row, so uptime can be counted from them."""
    last = await session.scalar(select(func.max(NodeHealth.at)).where(NodeHealth.node_id == node.id))
    if last is not None and now - last < HEALTH_MIN_INTERVAL:
        return False
    session.add(
        NodeHealth(
            node_id=node.id,
            at=now,
            gpu_temp_c=_num(health.gpu_temp_c) if health else None,
            gpu_pct=_num(health.gpu_pct) if health else None,
            disk_free_bytes=disk_free_bytes,
            busy=busy,
        )
    )
    return True


async def prune_health(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(delete(NodeHealth).where(NodeHealth.at < now - HEALTH_KEEP))
    return result.rowcount or 0
