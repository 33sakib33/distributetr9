"""Rate limits (B29): in-process token buckets.

A bucket holds up to `per_minute` tokens and refills at per_minute/60 a second,
so a burst of `per_minute` requests is fine and then requests are spaced out.
Buckets are keyed by the signed-in user, or by the client address when nobody
is signed in. State lives in this process (one uvicorn worker), which is what
the lab server runs; a restart forgets it, which is harmless.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable

from fastapi import Cookie, Depends, Header, Request

from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.models import User

# Idle full buckets are dropped once the table grows past this.
MAX_BUCKETS = 10_000


class RateLimited(AppError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            429,
            "rate_limited",
            f"Too many requests; try again in {retry_after} seconds",
            retry_after_seconds=retry_after,
        )
        self.headers = {"Retry-After": str(retry_after)}


class TokenBuckets:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}

    def take(self, name: str, key: str, per_minute: int) -> int:
        """Spend a token. Returns 0 when allowed, else the seconds to wait."""
        now = self.clock()
        rate = per_minute / 60.0
        tokens, updated = self._buckets.get((name, key), (float(per_minute), now))
        tokens = min(float(per_minute), tokens + (now - updated) * rate)
        if tokens >= 1.0:
            self._buckets[(name, key)] = (tokens - 1.0, now)
            self._trim(now)
            return 0
        self._buckets[(name, key)] = (tokens, now)
        return max(1, math.ceil((1.0 - tokens) / rate))

    def _trim(self, now: float) -> None:
        if len(self._buckets) <= MAX_BUCKETS:
            return
        # A bucket idle for two minutes is full again, so forgetting it changes nothing.
        for key in [k for k, (_t, updated) in self._buckets.items() if now - updated > 120]:
            del self._buckets[key]

    def clear(self) -> None:
        self._buckets.clear()


buckets = TokenBuckets()


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so behind Caddy this is the real client.
    return request.client.host if request.client else "unknown"


def _check(name: str, key: str, per_minute: int) -> None:
    if not settings.rate_limit_enabled or per_minute <= 0:
        return
    wait = buckets.take(name, key, per_minute)
    if wait:
        raise RateLimited(wait)


def limit_user(name: str, setting: str) -> Callable:
    """Dependency: `setting` requests a minute per signed-in user."""

    async def dependency(user: User = Depends(get_current_user)) -> None:
        _check(name, f"user:{user.id}", getattr(settings, setting))

    return dependency


def limit_ip(name: str, setting: str) -> Callable:
    """Dependency: `setting` requests a minute per client address."""

    async def dependency(request: Request) -> None:
        _check(name, f"ip:{client_ip(request)}", getattr(settings, setting))

    return dependency


def limit_user_or_ip(name: str, setting: str) -> Callable:
    """Dependency for endpoints that anyone may call: signed-in callers are
    counted as themselves, the rest by address."""

    async def dependency(
        request: Request,
        session=Depends(get_session),
        x_dev_user: str | None = Header(default=None, alias="X-Dev-User"),
        authorization: str | None = Header(default=None),
        lab_session: str | None = Cookie(default=None),
    ) -> None:
        try:
            user = await get_current_user(session, x_dev_user, authorization, lab_session)
            key = f"user:{user.id}"
        except AppError:
            key = f"ip:{client_ip(request)}"
        _check(name, key, getattr(settings, setting))

    return dependency
