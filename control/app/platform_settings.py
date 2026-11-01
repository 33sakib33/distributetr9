"""Operator-editable settings stored in the database (B20).

Today these are the agent rollout knobs. The environment variables
(AGENT_TARGET_VERSION, AGENT_ROLLOUT_RING, AGENT_MIN_VERSION) stay as the
defaults: a row in `platform_settings` overrides one, deleting the row brings
the default back.

Reads go through a small in-process cache so the heartbeat and claim paths do
not query for every request. A request calls `ensure_loaded` once and then reads
with the plain functions below. The cache is refreshed after CACHE_SECONDS and
cleared at once by `set_overrides` (and by tests via `clear_cache`); with more
than one server process the others pick a change up within CACHE_SECONDS.
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import PlatformSetting

CACHE_SECONDS = 10.0
KEYS = ("agent_target_version", "agent_rollout_ring", "agent_min_version")

_cache: dict[str, Any] = {}
_loaded_at: float | None = None


def clear_cache() -> None:
    global _loaded_at
    _cache.clear()
    _loaded_at = None


async def ensure_loaded(session: AsyncSession) -> None:
    global _loaded_at
    if _loaded_at is not None and time.monotonic() - _loaded_at < CACHE_SECONDS:
        return
    rows = (await session.scalars(select(PlatformSetting))).all()
    fresh = {row.key: row.value for row in rows}
    _cache.clear()
    _cache.update(fresh)
    _loaded_at = time.monotonic()


def overrides() -> dict[str, Any]:
    """The stored overrides (cached); a key without a row is absent."""
    return dict(_cache)


def target_version() -> str:
    return str(_cache.get("agent_target_version") or settings.agent_target_version or "")


def min_version() -> str:
    return str(_cache.get("agent_min_version") or settings.agent_min_version or "")


def rollout_ring() -> int:
    value = _cache.get("agent_rollout_ring")
    return int(value) if value is not None else settings.agent_rollout_ring


async def set_overrides(session: AsyncSession, values: dict[str, Any], updated_by: str) -> None:
    """Store a value, or delete the row when it is None. The caller commits and
    then calls clear_cache()."""
    for key, value in values.items():
        if key not in KEYS:
            raise ValueError(f"unknown setting {key}")
        if value is None:
            await session.execute(delete(PlatformSetting).where(PlatformSetting.key == key))
            continue
        row = await session.get(PlatformSetting, key)
        if row is None:
            session.add(PlatformSetting(key=key, value=value, updated_by=updated_by))
        else:
            row.value = value
            row.updated_by = updated_by
            row.updated_at = func.now()
    await session.flush()
