"""Availability schedules (B17): weekly windows, in LAB_TIMEZONE, when a room or
one PC is closed to jobs ("class hours").

The scheduler asks three questions of a `Schedule` loaded at the start of a
claim: is this PC inside a window now, until when, and would a job with this
maximum runtime run into the next window. The time comes from `clock()`, one
function tests replace to freeze the day; everything else is pure.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import AvailabilityWindow, Node

log = logging.getLogger("lab")

MINUTES_PER_DAY = 24 * 60
# A job is held back only when the next window opens within this horizon.
LOOKAHEAD = timedelta(hours=24)


def clock() -> datetime:
    """The lab's idea of now. Tests replace this to pin the weekday and time."""
    return datetime.now(timezone.utc)


def lab_zone() -> tzinfo:
    try:
        return ZoneInfo(settings.lab_timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        log.error("LAB_TIMEZONE %r is not a known time zone; using UTC", settings.lab_timezone)
        return timezone.utc


def zone_name() -> str:
    return getattr(lab_zone(), "key", None) or "UTC"


def hhmm(minute: int) -> str:
    minute = max(0, min(minute, MINUTES_PER_DAY))
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass(frozen=True)
class Window:
    id: uuid.UUID | None
    room: str | None
    node_id: uuid.UUID | None
    weekday: int
    start_minute: int
    end_minute: int

    def covers(self, node: Node) -> bool:
        if self.node_id is not None:
            return self.node_id == node.id
        return bool(self.room and node.room and self.room.strip().lower() == node.room.strip().lower())


@dataclass
class Schedule:
    """The windows and the moment they are judged at."""

    windows: list[Window] = field(default_factory=list)
    at: datetime = field(default_factory=lambda: clock())
    zone: tzinfo = field(default_factory=lab_zone)

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            self.at = self.at.replace(tzinfo=timezone.utc)

    def _for(self, node: Node) -> list[Window]:
        return [w for w in self.windows if w.covers(node)]

    def _local(self) -> datetime:
        return self.at.astimezone(self.zone)

    def _day_start(self, local_date, minute: int) -> datetime:
        base = datetime.combine(local_date, time.min, tzinfo=self.zone)
        # Wall-clock arithmetic: 08:00 stays 08:00 across a daylight-saving change.
        hour, mins = divmod(min(minute, MINUTES_PER_DAY), 60)
        if hour >= 24:
            return datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=self.zone)
        return base.replace(hour=hour, minute=mins)

    def current_end(self, node: Node) -> datetime | None:
        """When the window the PC is in now ends (back-to-back windows count as
        one), or None when it is not in a window."""
        mine = self._for(node)
        local = self._local()
        minute = local.hour * 60 + local.minute
        hit = next((w for w in mine if w.weekday == local.weekday() and w.start_minute <= minute < w.end_minute), None)
        if hit is None:
            return None
        day = local.date()
        end_minute = hit.end_minute
        weekday = hit.weekday
        for _ in range(7 * 2):  # follow adjacent windows, at most two weeks
            if end_minute < MINUTES_PER_DAY:
                follower = next((w for w in mine if w.weekday == weekday and w.start_minute == end_minute), None)
            else:
                day += timedelta(days=1)
                weekday = (weekday + 1) % 7
                end_minute = 0
                follower = next((w for w in mine if w.weekday == weekday and w.start_minute == 0), None)
            if follower is None:
                break
            end_minute = follower.end_minute
        return self._day_start(day, end_minute)

    def blocked(self, node: Node) -> bool:
        return self.current_end(node) is not None

    def next_start(self, node: Node) -> datetime | None:
        """The next time a window opens, if that is within LOOKAHEAD."""
        mine = self._for(node)
        if not mine:
            return None
        today = self._local().date()
        best: datetime | None = None
        for offset in range(0, 3):
            day = today + timedelta(days=offset)
            for w in mine:
                if w.weekday != day.weekday():
                    continue
                start = self._day_start(day, w.start_minute)
                # Compared as instants, so a daylight-saving change does not skew the 24 h.
                if start > self.at and (best is None or start < best):
                    best = start
        if best is not None and best - self.at <= LOOKAHEAD:
            return best
        return None

    def collision(self, node: Node, max_runtime_seconds: int) -> datetime | None:
        """The window start a job of this length would run into, or None."""
        start = self.next_start(node)
        if start is None:
            return None
        if self.at + timedelta(seconds=max_runtime_seconds) >= start:
            return start
        return None

    def reserved_reason(self, nodes: list[Node]) -> str | None:
        """Why work waits when every one of `nodes` is inside a window."""
        ends = [end for n in nodes if (end := self.current_end(n)) is not None]
        if not nodes or len(ends) != len(nodes):
            return None
        return f"lab reserved for teaching until {min(ends):%H:%M}"


EMPTY = Schedule(windows=[])


def _window(row: AvailabilityWindow) -> Window:
    return Window(row.id, row.room, row.node_id, row.weekday, row.start_minute, row.end_minute)


async def load_schedule(session: AsyncSession) -> Schedule:
    rows = (await session.scalars(select(AvailabilityWindow))).all()
    return Schedule(windows=[_window(r) for r in rows], at=clock(), zone=lab_zone())
