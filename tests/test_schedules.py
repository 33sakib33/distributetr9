"""Availability schedules (B17): class-hour windows close a room or a PC to
jobs, and hold back jobs that would run into the next window. The clock is
frozen by replacing `app.schedule.clock`."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app import schedule
from app.config import settings
from app.schedule import Schedule, Window
from harness.client import FakeAgent
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}
MON = "2026-11-02"  # a Monday
TUE = "2026-11-03"


def _caps(tag: str) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}


def _node(client, tag: str, room: str | None = None, name: str = "pc") -> FakeAgent:
    agent = FakeAgent(client, hostname=f"sched-{name}-{tag}", caps=_caps(tag))
    agent.register(room=room)
    agent.heartbeat()
    return agent


def _minutes(hhmm: str) -> int:
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


@pytest.fixture
def windows(client):
    """Create windows through the API and delete them afterwards, so a window
    never leaks into another test."""
    made: list[str] = []

    def add(start: str, end: str, weekday: int = 0, **target) -> dict:
        body = {"weekday": weekday, "start_minute": _minutes(start), "end_minute": _minutes(end), **target}
        resp = client.post("/admin/schedules", json=body, headers=ADMIN)
        assert resp.status_code == 201, resp.text
        made.append(resp.json()["id"])
        return resp.json()

    yield add
    for window_id in made:
        client.delete(f"/admin/schedules/{window_id}", headers=ADMIN)


@pytest.fixture
def at(monkeypatch):
    def freeze(stamp: str) -> None:
        monkeypatch.setattr(schedule, "clock", lambda: datetime.fromisoformat(stamp))

    return freeze


def test_a_node_inside_its_rooms_window_gets_no_work_and_the_job_says_why(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("09:00", "11:00", room=room)
    node = _node(client, tag, room=room)
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)

    at(f"{MON}T09:30:00+00:00")
    assert node.claim() is None
    assert portal.get_job(job["id"])["pending_reason"] == "lab reserved for teaching until 11:00"

    at(f"{MON}T11:00:00+00:00")  # the end of a window is outside it
    attempt = node.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]


def test_the_room_match_ignores_case_and_other_rooms_are_not_blocked(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    windows("09:00", "11:00", room=f"lab {tag}")
    shouting = _node(client, tag, room=f"LAB {tag}", name="a")
    elsewhere = _node(client, tag, room=f"Other {tag}", name="b")
    unplaced = _node(client, tag, name="c")
    portal = Portal(client)
    jobs = [portal.submit_job(gpu_class=tag)["id"] for _ in range(2)]

    at(f"{MON}T09:30:00+00:00")
    assert shouting.claim() is None
    first = elsewhere.claim()
    second = unplaced.claim()
    assert first is not None and second is not None
    assert {first["job_id"], second["job_id"]} == set(jobs)


def test_a_window_can_close_one_pc(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    closed = _node(client, tag, name="closed")
    open_pc = _node(client, tag, name="open")
    windows("09:00", "11:00", node_id=closed.node_id)
    job = Portal(client).submit_job(gpu_class=tag)

    at(f"{MON}T10:00:00+00:00")
    assert closed.claim() is None
    attempt = open_pc.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]


def test_a_job_that_would_run_into_class_hours_is_skipped_for_a_shorter_one(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("13:00", "15:00", room=room)
    node = _node(client, tag, room=room)
    portal = Portal(client)
    long_job = portal.submit_job(gpu_class=tag, max_runtime_seconds=3 * 3600)
    short_job = portal.submit_job(gpu_class=tag, max_runtime_seconds=3600)

    at(f"{MON}T11:00:00+00:00")
    attempt = node.claim()
    assert attempt is not None and attempt["job_id"] == short_job["id"]  # older long job is passed over
    node.run_to_complete(attempt)

    assert node.claim() is None
    reason = portal.get_job(long_job["id"])["pending_reason"]
    assert reason.startswith("lab reserved for teaching from 13:00")

    at(f"{MON}T15:00:00+00:00")  # next window is a week away
    after = node.claim()
    assert after is not None and after["job_id"] == long_job["id"]


def test_a_job_that_ends_exactly_when_the_window_opens_is_too_long(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("13:00", "15:00", room=room)
    node = _node(client, tag, room=room)
    portal = Portal(client)
    exact = portal.submit_job(gpu_class=tag, max_runtime_seconds=7200)  # 11:00 + 2 h = 13:00
    fits = portal.submit_job(gpu_class=tag, max_runtime_seconds=7199)

    at(f"{MON}T11:00:00+00:00")
    attempt = node.claim()
    assert attempt is not None and attempt["job_id"] == fits["id"]
    assert exact["id"] != fits["id"]


def test_only_a_window_within_24_hours_holds_a_job_back(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("10:00", "12:00", weekday=1, room=room)  # Tuesday
    node = _node(client, tag, room=room)
    portal = Portal(client)
    day_long = portal.submit_job(gpu_class=tag, max_runtime_seconds=86_400)
    shorter = portal.submit_job(gpu_class=tag, max_runtime_seconds=82_000)  # ends before Tue 10:00

    at(f"{MON}T11:00:00+00:00")  # Tuesday 10:00 is 23 h away
    attempt = node.claim()
    assert attempt is not None and attempt["job_id"] == shorter["id"]
    node.run_to_complete(attempt)
    assert node.claim() is None

    at(f"{MON}T09:00:00+00:00")  # 25 h away: out of the horizon, a day-long job may start
    later = node.claim()
    assert later is not None and later["job_id"] == day_long["id"]


def test_back_to_back_windows_across_midnight_close_the_room_until_the_last_ends(client, windows, at):
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("22:00", "24:00", weekday=0, room=room)
    windows("00:00", "02:00", weekday=1, room=room)
    node = _node(client, tag, room=room)
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)

    at(f"{MON}T23:00:00+00:00")
    assert node.claim() is None
    assert portal.get_job(job["id"])["pending_reason"] == "lab reserved for teaching until 02:00"

    at(f"{TUE}T01:59:00+00:00")
    assert node.claim() is None
    at(f"{TUE}T02:00:00+00:00")
    assert node.claim() is not None


def test_windows_are_read_in_the_lab_timezone(client, windows, at, monkeypatch):
    monkeypatch.setattr(settings, "lab_timezone", "Asia/Dhaka")  # UTC+6
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("09:00", "11:00", room=room)
    node = _node(client, tag, room=room)
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)

    at(f"{MON}T03:30:00+00:00")  # 09:30 in Dhaka
    assert node.claim() is None
    assert portal.get_job(job["id"])["pending_reason"] == "lab reserved for teaching until 11:00"

    at(f"{MON}T09:30:00+00:00")  # 15:30 in Dhaka
    assert node.claim() is not None
    listed = client.get("/admin/schedules", headers=ADMIN).json()
    assert listed["timezone"] == "Asia/Dhaka"


def test_the_weekday_is_the_local_weekday(client, windows, at, monkeypatch):
    monkeypatch.setattr(settings, "lab_timezone", "Asia/Dhaka")
    tag = uuid.uuid4().hex[:8]
    room = f"Lab {tag}"
    windows("02:00", "03:00", weekday=0, room=room)  # Monday 02:00 Dhaka = Sunday 20:00 UTC
    node = _node(client, tag, room=room)
    Portal(client).submit_job(gpu_class=tag)

    at("2026-11-01T20:30:00+00:00")
    assert node.claim() is None


def test_node_can_claim_names_the_schedule():
    from app.scheduler import node_can_claim

    now = datetime.now(timezone.utc)
    node = SimpleNamespace(
        id=uuid.uuid4(), room="Lab 1", eligibility="eligible", last_heartbeat_at=now, decommissioned_at=None,
        gpu_desktop_busy=False, last_health=None, disk_free_bytes=10**12, agent_version="0.0.0",
    )
    closed = Schedule([Window(None, "lab 1", None, 0, 0, 1440)], at=datetime.fromisoformat(f"{MON}T12:00:00+00:00"))
    assert node_can_claim(node, now) is None
    assert node_can_claim(node, now, closed) == "schedule"
    node.eligibility = "draining"  # a more basic reason wins
    assert node_can_claim(node, now, closed) == "draining"


def test_the_24_hour_horizon_is_measured_in_real_hours_across_a_clock_change(monkeypatch):
    # New York falls back on Sunday 2026-11-01: Saturday 16:00 EDT to Sunday 16:00 EST is 25 hours.
    monkeypatch.setattr(settings, "lab_timezone", "America/New_York")
    node = SimpleNamespace(id=uuid.uuid4(), room="x")
    sunday_four_pm = Window(None, "x", None, 6, 16 * 60, 17 * 60)
    saturday_afternoon = datetime.fromisoformat("2026-10-31T20:00:00+00:00")  # 16:00 EDT
    assert Schedule([sunday_four_pm], at=saturday_afternoon).next_start(node) is None
    nearly = saturday_afternoon + timedelta(hours=1, minutes=1)
    assert Schedule([sunday_four_pm], at=nearly).next_start(node) is not None


def test_schedule_crud_and_validation(client):
    room = f"Lab {uuid.uuid4().hex[:6]}"
    body = {"room": room, "weekday": 2, "start_minute": 540, "end_minute": 660, "note": "CSE 101"}
    created = client.post("/admin/schedules", json=body, headers=ADMIN)
    assert created.status_code == 201, created.text
    window = created.json()
    assert window["room"] == room and window["node_id"] is None and window["note"] == "CSE 101"
    assert window["start"] == "09:00" and window["end"] == "11:00"

    listed = client.get("/admin/schedules", params={"room": room.lower()}, headers=ADMIN).json()
    assert [w["id"] for w in listed["items"]] == [window["id"]] and listed["next_cursor"] is None

    patched = client.patch(f"/admin/schedules/{window['id']}", json={"end_minute": 720, "note": ""}, headers=ADMIN)
    assert patched.status_code == 200, patched.text
    assert patched.json()["end"] == "12:00" and patched.json()["note"] is None

    bad = {
        "ends before it starts": {"start_minute": 600, "end_minute": 600},
        "crosses midnight": {"start_minute": 1320, "end_minute": 120},
        "no target": {"room": None},
        "two targets": {"node_id": str(uuid.uuid4())},
    }
    for label, change in bad.items():
        resp = client.post("/admin/schedules", json={**body, **change}, headers=ADMIN)
        assert resp.status_code in (400, 404, 422), (label, resp.text)
    assert client.post("/admin/schedules", json={**body, "weekday": 7}, headers=ADMIN).status_code == 422
    assert client.post("/admin/schedules", json={**body, "end_minute": 1441}, headers=ADMIN).status_code == 422
    unknown_pc = client.post("/admin/schedules", json={"node_id": str(uuid.uuid4()), "weekday": 1, "start_minute": 0, "end_minute": 60}, headers=ADMIN)
    assert unknown_pc.status_code == 404
    both = client.post("/admin/schedules", json={**body, "node_id": str(uuid.uuid4())}, headers=ADMIN)
    assert both.status_code == 400
    assert client.patch(f"/admin/schedules/{window['id']}", json={"start_minute": 800}, headers=ADMIN).status_code == 400

    assert client.delete(f"/admin/schedules/{window['id']}", headers=ADMIN).status_code == 200
    assert client.delete(f"/admin/schedules/{window['id']}", headers=ADMIN).status_code == 404
    assert client.patch(f"/admin/schedules/{window['id']}", json={"note": "x"}, headers=ADMIN).status_code == 404


def test_naming_a_pc_moves_a_window_off_its_room(client):
    tag = uuid.uuid4().hex[:8]
    node = _node(client, tag)
    body = {"room": f"Lab {tag}", "weekday": 0, "start_minute": 60, "end_minute": 120}
    window = client.post("/admin/schedules", json=body, headers=ADMIN).json()
    moved = client.patch(f"/admin/schedules/{window['id']}", json={"node_id": node.node_id}, headers=ADMIN)
    assert moved.status_code == 200, moved.text
    assert moved.json()["room"] is None and moved.json()["node_id"] == node.node_id
    assert moved.json()["hostname"] == node.hostname
    client.delete(f"/admin/schedules/{window['id']}", headers=ADMIN)


def test_schedules_are_for_admins(client, monkeypatch):
    client.get("/me", headers=ADMIN)  # the admin must exist before new users stop being admins
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/schedules", headers=student).status_code == 403
    body = {"room": "x", "weekday": 0, "start_minute": 0, "end_minute": 60}
    assert client.post("/admin/schedules", json=body, headers=student).status_code == 403
