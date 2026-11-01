"""Usage reports (B12): attempts with known start and end give known totals."""
from __future__ import annotations

import csv
import io
import itertools
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models import JobAttempt, NodeHealth
from harness.client import FakeAgent
from harness.jobs import Portal

_days = itertools.count(12, 5)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


class Day:
    """A one-day window of its own, days back and well apart (a test may place
    attempts a day either side), so no other test's attempts or
    health samples land in it."""

    def __init__(self) -> None:
        self.base = NOW - timedelta(days=next(_days))
        self.window = {"from": self.base.isoformat(), "to": (self.base + timedelta(days=1)).isoformat()}

    def at(self, hours: float) -> datetime:
        return self.base + timedelta(hours=hours)


async def _with_session(fn):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as session:
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _place(attempt_id: str, start: datetime, end: datetime | None) -> None:
    async def move(session):
        await session.execute(
            update(JobAttempt)
            .where(JobAttempt.id == uuid.UUID(attempt_id))
            .values(started_at=start, completed_at=end)
        )

    await _with_session(move)


def _lab_agent(client, tag: str) -> FakeAgent:
    caps = {
        "compatibility_class": tag,
        "gpu_model": tag,
        "vram_total_mb": 8192,
        "vram_allocatable_mb": 8000,
    }
    agent = FakeAgent(client, hostname=f"use-{tag}", caps=caps)
    agent.register()
    agent.heartbeat()
    return agent


async def _run(
    day: Day,
    portal: Portal,
    agent: FakeAgent,
    *,
    project_id: str,
    tag: str,
    start_h: float,
    end_h: float,
    min_vram_mb: int = 4000,
    fail: bool = False,
) -> str:
    job = portal.submit_job(gpu_class=tag, min_vram_mb=min_vram_mb, project_id=project_id)
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    done = agent.complete(attempt, exit_code=1 if fail else 0, failure_category="user_code" if fail else None)
    assert done["status_code"] == 200, done
    await _place(attempt["attempt_id"], day.at(start_h), day.at(end_h))
    return job["id"]


def _rows(resp, by: str = "label") -> dict[str, dict]:
    assert resp.status_code == 200, resp.text
    return {row[by]: row for row in resp.json()["rows"]}


async def _fixture(client, day: Day):
    """Alice: 2 h + 0.5 h (failed) of GPU, 1 h of CPU-only. Bob: 1 h of GPU."""
    tag = f"use-{uuid.uuid4().hex[:8]}"
    agent = _lab_agent(client, tag)
    alice, bob = Portal(client), Portal(client)
    alice_project = alice.ensure_project(f"alice-{tag}")
    bob_project = bob.ensure_project(f"bob-{tag}")
    await _run(day, alice, agent, project_id=alice_project, tag=tag, start_h=1, end_h=3)
    await _run(day, alice, agent, project_id=alice_project, tag=tag, start_h=4, end_h=4.5, fail=True)
    await _run(day, alice, agent, project_id=alice_project, tag=tag, start_h=5, end_h=6, min_vram_mb=0)
    await _run(day, bob, agent, project_id=bob_project, tag=tag, start_h=7, end_h=8)
    return tag, alice, bob, alice_project, bob_project


async def test_usage_by_user_has_known_totals(client):
    day = Day()
    _tag, alice, bob, *_ = await _fixture(client, day)
    body = client.get("/admin/usage", params={**day.window, "group": "user"}, headers=alice.headers)
    rows = _rows(body)
    assert rows[alice.email] == {
        "key": rows[alice.email]["key"],
        "label": alice.email,
        "gpu_hours": 2.5,  # the CPU-only hour does not count
        "jobs": 3,
        "succeeded": 2,
        "failed": 1,
    }
    assert rows[bob.email]["gpu_hours"] == 1.0 and rows[bob.email]["jobs"] == 1 and rows[bob.email]["succeeded"] == 1
    assert body.json()["group"] == "user"
    keys = [r["key"] for r in body.json()["rows"]]
    assert len(set(keys)) == len(keys)


async def test_usage_by_project_and_by_class(client):
    day = Day()
    tag, alice, bob, alice_project, bob_project = await _fixture(client, day)
    by_project = client.get("/admin/usage", params={**day.window, "group": "project"}, headers=alice.headers)
    rows = _rows(by_project, by="key")
    assert rows[alice_project]["label"] == f"alice-{tag}"
    assert (rows[alice_project]["gpu_hours"], rows[alice_project]["jobs"]) == (2.5, 3)
    assert (rows[bob_project]["gpu_hours"], rows[bob_project]["jobs"]) == (1.0, 1)

    by_class = _rows(client.get("/admin/usage", params={**day.window, "group": "class"}, headers=alice.headers))
    assert by_class[tag]["gpu_hours"] == 3.5 and by_class[tag]["jobs"] == 4
    assert (by_class[tag]["succeeded"], by_class[tag]["failed"]) == (3, 1)


async def test_rows_are_sorted_by_gpu_hours(client):
    day = Day()
    _tag, alice, bob, *_ = await _fixture(client, day)
    labels = [r["label"] for r in client.get("/admin/usage", params={**day.window, "group": "user"}, headers=alice.headers).json()["rows"]]
    assert labels.index(alice.email) < labels.index(bob.email)


async def test_attempts_are_clipped_to_the_window_and_outside_ones_are_left_out(client):
    day = Day()
    tag = f"use-{uuid.uuid4().hex[:8]}"
    agent = _lab_agent(client, tag)
    portal = Portal(client)
    project = portal.ensure_project(f"clip-{tag}")
    await _run(day, portal, agent, project_id=project, tag=tag, start_h=-1, end_h=1)  # half inside
    await _run(day, portal, agent, project_id=project, tag=tag, start_h=-30, end_h=-28)  # before the window
    await _run(day, portal, agent, project_id=project, tag=tag, start_h=30, end_h=31)  # after it
    rows = _rows(client.get("/admin/usage", params={**day.window, "group": "class"}, headers=portal.headers))
    assert rows[tag]["gpu_hours"] == 1.0 and rows[tag]["jobs"] == 1


async def test_lab_hours_come_from_health_samples_and_attempt_time(client):
    day = Day()
    tag, alice, _bob, *_ = await _fixture(client, day)  
    node_agent = FakeAgent(client, hostname=f"online-{tag}")
    node_agent.register()
    builder = FakeAgent(client, hostname=f"build-{tag}", role="build")
    builder.register()

    async def samples(session):
        for node, count in ((node_agent, 120), (builder, 600)):
            session.add_all(
                NodeHealth(node_id=uuid.UUID(node.node_id), at=day.base + timedelta(minutes=i), busy=False)
                for i in range(count)
            )

    await _with_session(samples)
    lab = client.get("/admin/usage", params=day.window, headers=alice.headers).json()["lab"]
    # Earlier tests' PCs have no samples that far back, and builders are not counted.
    assert lab["online_hours"] == 2.0
    assert lab["busy_hours"] == 4.5  # 2 + .5 + 1 + 1: CPU-only attempts are busy time too
    assert lab["utilisation"] == 1.0  # capped: more busy than online here


async def test_utilisation_is_busy_over_online(client):
    day = Day()
    tag = f"use-{uuid.uuid4().hex[:8]}"
    agent = _lab_agent(client, tag)
    portal = Portal(client)
    await _run(day, portal, agent, project_id=portal.ensure_project(f"u-{tag}"), tag=tag, start_h=1, end_h=2)

    async def samples(session):
        session.add_all(
            NodeHealth(node_id=uuid.UUID(agent.node_id), at=day.base + timedelta(minutes=i), busy=False) for i in range(240)
        )

    await _with_session(samples)
    lab = client.get("/admin/usage", params=day.window, headers=portal.headers).json()["lab"]
    assert (lab["online_hours"], lab["busy_hours"], lab["utilisation"]) == (4.0, 1.0, 0.25)


def test_an_empty_window_has_no_rows_and_zero_utilisation(client):
    far = {"from": "2001-01-01", "to": "2001-01-02"}
    body = client.get("/admin/usage", params=far, headers=Portal(client).headers).json()
    assert body["rows"] == []
    assert body["lab"] == {"online_hours": 0.0, "busy_hours": 0.0, "utilisation": 0.0}


async def test_my_usage_is_only_my_jobs_by_project(client):
    day = Day()
    tag, alice, bob, alice_project, bob_project = await _fixture(client, day)
    mine = _rows(client.get("/me/usage", params=day.window, headers=alice.headers), by="key")
    assert set(mine) == {alice_project}
    assert (mine[alice_project]["gpu_hours"], mine[alice_project]["jobs"], mine[alice_project]["failed"]) == (2.5, 3, 1)
    theirs = _rows(client.get("/me/usage", params=day.window, headers=bob.headers), by="key")
    assert set(theirs) == {bob_project}
    assert "lab" not in client.get("/me/usage", params=day.window, headers=bob.headers).json()


async def test_csv_export_for_both_endpoints(client):
    day = Day()
    tag, alice, _bob, alice_project, _ = await _fixture(client, day)
    admin = client.get("/admin/usage", params={**day.window, "group": "user", "format": "csv"}, headers=alice.headers)
    assert admin.status_code == 200
    assert admin.headers["content-type"].startswith("text/csv")
    assert "attachment" in admin.headers["content-disposition"]
    table = list(csv.reader(io.StringIO(admin.text)))
    assert table[0] == ["key", "label", "gpu_hours", "jobs", "succeeded", "failed"]
    ours = next(r for r in table[1:] if r[1] == alice.email)
    assert ours[2:] == ["2.5", "3", "2", "1"]

    mine = client.get("/me/usage", params={**day.window, "format": "csv"}, headers=alice.headers)
    table = list(csv.reader(io.StringIO(mine.text)))
    assert [r[0] for r in table[1:]] == [alice_project]


async def test_csv_does_not_let_a_project_name_become_a_formula(client):
    day = Day()
    tag = f"use-{uuid.uuid4().hex[:8]}"
    agent = _lab_agent(client, tag)
    portal = Portal(client)
    project = portal.ensure_project(f"=HYPERLINK(\"http://x\",\"{tag}\")")
    await _run(day, portal, agent, project_id=project, tag=tag, start_h=1, end_h=2)
    resp = client.get("/me/usage", params={**day.window, "format": "csv"}, headers=portal.headers)
    label = list(csv.reader(io.StringIO(resp.text)))[1][1]
    assert label.startswith("'=HYPERLINK")


def test_bad_parameters_are_400s(client):
    h = Portal(client).headers
    assert client.get("/admin/usage", params={"group": "planet"}, headers=h).status_code == 400
    assert client.get("/admin/usage", params={"from": "yesterday"}, headers=h).status_code == 400
    assert client.get("/admin/usage", params={"from": "2026-02-02", "to": "2026-02-01"}, headers=h).status_code == 400
    assert client.get("/me/usage", params={"from": "2026-02-02", "to": "2026-02-01"}, headers=h).status_code == 400


def test_a_date_only_end_covers_that_whole_day(client):
    body = client.get("/admin/usage", params={"from": "2026-02-01", "to": "2026-02-01"}, headers=Portal(client).headers)
    assert body.status_code == 200
    assert body.json()["from"].startswith("2026-02-01T00:00:00")
    assert body.json()["to"].startswith("2026-02-02T00:00:00")


def test_default_window_is_the_last_thirty_days(client):
    body = client.get("/admin/usage", headers=Portal(client).headers).json()
    begin, end = datetime.fromisoformat(body["from"]), datetime.fromisoformat(body["to"])
    assert end - begin == timedelta(days=30)


def test_usage_endpoints_need_the_right_role(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "dev_auth_admin", False)
    stranger = {"X-Dev-User": f"stranger-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/usage", headers=stranger).status_code == 403
    assert client.get("/me/usage", headers=stranger).status_code == 200
    assert client.get("/me/usage").status_code == 401
