"""Prometheus metrics (B10) and the Grafana dashboard that reads them (B11)."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import metrics
from app.config import settings
from app.models import JobAttempt
from harness.client import FakeAgent
from harness.jobs import Portal
from tests.test_scheduler_races import isolated

ROOT = Path(__file__).resolve().parents[1]

GAUGES = {"lab_queue_jobs", "lab_nodes", "lab_running_attempts", "lab_builds"}
COUNTERS = {"lab_jobs_finished_total", "lab_lease_lost_total", "lab_claims_total"}
HISTOGRAMS = {"lab_time_to_start_seconds", "lab_image_pull_seconds", "lab_job_runtime_seconds"}


def scrape_text(client, **headers) -> str:
    resp = client.get("/metrics", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.text


def samples(text: str) -> dict[tuple[str, frozenset], float]:
    found = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            found[(sample.name, frozenset(sample.labels.items()))] = sample.value
    return found


def served_names(text: str) -> set[str]:
    """Every metric name a query could use: samples, plus labelled families that
    have no series yet (an empty queue has no `lab_queue_jobs` line)."""
    names = set()
    for family in text_string_to_metric_families(text):
        names.add(family.name)
        names.update(sample.name for sample in family.samples)
    return names


def value(client, name: str, **labels) -> float:
    return samples(scrape_text(client)).get((name, frozenset(labels.items())), 0.0)


# --- endpoint -------------------------------------------------------------------


def test_metrics_output_names_every_metric(client):
    text = scrape_text(client)
    names = served_names(text)
    assert GAUGES <= names
    assert COUNTERS <= names
    for histogram in HISTOGRAMS:
        assert {f"{histogram}_bucket", f"{histogram}_sum", f"{histogram}_count"} <= names
    for state in metrics.NODE_STATES:
        assert ("lab_nodes", frozenset({("state", state)})) in samples(text)
    for outcome in metrics.OUTCOMES:
        assert ("lab_jobs_finished_total", frozenset({("outcome", outcome)})) in samples(text)
    for result in metrics.CLAIM_RESULTS:
        assert ("lab_claims_total", frozenset({("result", result)})) in samples(text)


def test_metrics_is_plain_prometheus_text(client):
    resp = client.get("/metrics")
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# TYPE lab_nodes gauge" in resp.text


def test_metrics_token_is_enforced_when_set(client, monkeypatch):
    monkeypatch.setattr(settings, "metrics_token", "s3cret-token")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "s3cret-token"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret-token"}).status_code == 200
    assert client.get("/metrics", headers={"Authorization": "bearer s3cret-token"}).status_code == 200


def test_metrics_is_open_without_a_token(client):
    assert settings.metrics_token == ""
    assert client.get("/metrics").status_code == 200


# --- gauges ---------------------------------------------------------------------


def test_queue_gauge_counts_waiting_jobs_by_class(client):
    tag = f"q-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    portal.submit_job(gpu_class=tag)
    portal.submit_job(gpu_class=tag)
    assert value(client, "lab_queue_jobs", gpu_class=tag) == 2

    cancelled = portal.submit_job(gpu_class=tag)
    portal.cancel(cancelled["id"])
    assert value(client, "lab_queue_jobs", gpu_class=tag) == 2


def test_queue_gauge_forgets_a_class_once_it_drains(client):
    tag = f"q-{uuid.uuid4().hex[:8]}"
    job = Portal(client).submit_job(gpu_class=tag)
    assert value(client, "lab_queue_jobs", gpu_class=tag) == 1
    Portal(client).cancel(job["id"])
    assert ("lab_queue_jobs", frozenset({("gpu_class", tag)})) not in samples(scrape_text(client))


def test_node_and_running_gauges_follow_the_fleet(client):
    before = samples(scrape_text(client))
    portal, agent, job_id = isolated(client)
    idle_now = value(client, "lab_nodes", state="idle")
    assert idle_now == before.get(("lab_nodes", frozenset({("state", "idle")})), 0) + 1

    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    assert value(client, "lab_nodes", state="busy") == before.get(("lab_nodes", frozenset({("state", "busy")})), 0) + 1
    assert value(client, "lab_nodes", state="idle") == idle_now - 1
    assert value(client, "lab_running_attempts") == before.get(("lab_running_attempts", frozenset()), 0) + 1

    agent.event(attempt, "uploading")
    agent.complete(attempt)
    assert value(client, "lab_running_attempts") == before.get(("lab_running_attempts", frozenset()), 0)


def test_drained_and_quarantined_nodes_have_their_own_states(client):
    before = {s: value(client, "lab_nodes", state=s) for s in metrics.NODE_STATES}
    admin = Portal(client).headers
    a = FakeAgent(client, hostname=f"m-{uuid.uuid4().hex[:8]}")
    b = FakeAgent(client, hostname=f"m-{uuid.uuid4().hex[:8]}")
    for agent in (a, b):
        agent.register()
        agent.heartbeat()
    assert client.post(f"/nodes/{a.node_id}/drain", headers=admin).status_code == 200
    assert client.post(f"/nodes/{b.node_id}/quarantine", json={"reason": "test"}, headers=admin).status_code == 200
    assert value(client, "lab_nodes", state="draining") == before["draining"] + 1
    assert value(client, "lab_nodes", state="quarantined") == before["quarantined"] + 1


def test_a_node_with_a_stale_heartbeat_counts_as_offline(client):
    agent = FakeAgent(client, hostname=f"m-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    before = {s: value(client, "lab_nodes", state=s) for s in ("idle", "offline")}
    _backdate_heartbeat(agent.node_id)
    assert value(client, "lab_nodes", state="idle") == before["idle"] - 1
    assert value(client, "lab_nodes", state="offline") == before["offline"] + 1


def _backdate_heartbeat(node_id: str) -> None:
    import asyncio
    import concurrent.futures

    from app.models import Node

    async def go():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine) as session:
                await session.execute(
                    update(Node)
                    .where(Node.id == uuid.UUID(node_id))
                    .values(last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1))
                )
                await session.commit()
        finally:
            await engine.dispose()

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, go()).result()


def test_build_gauge_counts_by_state(client):
    tag = f"mb-{uuid.uuid4().hex[:6]}"
    before = {s: value(client, "lab_builds", state=s) for s in ("pending", "failed")}
    Portal(client).submit_job(gpu_class=tag, requirements=f"pkg-{uuid.uuid4().hex[:8]}==1.0\n")
    assert value(client, "lab_builds", state="pending") == before["pending"] + 1

    # Fail every waiting build, so none is left for other tests' builders to find.
    builder = FakeAgent(client, hostname=f"mb-{tag}", role="build")
    builder.register()
    builder.heartbeat()
    failed = 0
    while True:
        claimed = client.post("/agent/claim", json={}, headers=builder._headers()).json().get("build")
        if claimed is None:
            break
        done = client.post(
            f"/agent/builds/{claimed['build_id']}/complete", json={"ok": False, "error": "test"}, headers=builder._headers()
        )
        assert done.status_code == 200, done.text
        failed += 1
    assert failed >= 1
    assert value(client, "lab_builds", state="pending") == 0
    assert value(client, "lab_builds", state="failed") == before["failed"] + failed


# --- counters and histograms ----------------------------------------------------


def _done_job(client, *, exit_code: int = 0, category: str | None = None):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    return portal, agent, job_id, attempt, agent.complete(attempt, exit_code=exit_code, failure_category=category)


def test_completed_jobs_are_counted_once_with_runtime_and_time_to_start(client):
    before = {
        "done": value(client, "lab_jobs_finished_total", outcome="completed"),
        "claims": value(client, "lab_claims_total", result="job"),
        "start": value(client, "lab_time_to_start_seconds_count"),
        "run": value(client, "lab_job_runtime_seconds_count"),
    }
    _portal, agent, _job, attempt, done = _done_job(client)
    assert done["status_code"] == 200
    assert agent.complete(attempt)["status_code"] == 200  # a repeated complete is not a second job
    assert value(client, "lab_jobs_finished_total", outcome="completed") == before["done"] + 1
    assert value(client, "lab_claims_total", result="job") == before["claims"] + 1
    assert value(client, "lab_time_to_start_seconds_count") == before["start"] + 1
    assert value(client, "lab_job_runtime_seconds_count") == before["run"] + 1


def test_failed_jobs_are_counted_as_failed(client):
    before = value(client, "lab_jobs_finished_total", outcome="failed")
    _done_job(client, exit_code=1, category="user_code")
    assert value(client, "lab_jobs_finished_total", outcome="failed") == before + 1


def test_a_retried_failure_is_not_a_finished_job(client):
    before = {o: value(client, "lab_jobs_finished_total", outcome=o) for o in metrics.OUTCOMES}
    portal, agent, job_id, _attempt, done = _done_job(client, exit_code=1, category="node_lost")
    assert done["body"]["status"] == "queued"
    assert {o: value(client, "lab_jobs_finished_total", outcome=o) for o in metrics.OUTCOMES} == before
    # the retry is a second attempt: no second time-to-start sample
    start = value(client, "lab_time_to_start_seconds_count")
    again = agent.claim()
    assert again is not None and again["job_id"] == job_id
    assert value(client, "lab_time_to_start_seconds_count") == start
    # Leave nothing leased: its lease would expire a few seconds into a later test.
    assert agent.run_to_complete(again)["status_code"] == 200


def test_cancelled_queued_jobs_are_counted_once(client):
    before = value(client, "lab_jobs_finished_total", outcome="cancelled")
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"c-{uuid.uuid4().hex[:8]}")
    portal.cancel(job["id"])
    portal.cancel(job["id"])
    assert value(client, "lab_jobs_finished_total", outcome="cancelled") == before + 1


def test_empty_claims_are_counted(client):
    agent = FakeAgent(client, hostname=f"m-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    before = value(client, "lab_claims_total", result="empty")
    assert agent.claim() is None
    assert value(client, "lab_claims_total", result="empty") == before + 1


def _wait_until_no_attempt_is_active(client) -> None:
    """The lease worker fails any attempt a test abandoned within seconds, and
    each one adds to lab_lease_lost_total; let those land before counting."""
    deadline = time.time() + 15
    while time.time() < deadline and value(client, "lab_running_attempts") > 0:
        time.sleep(0.2)
    assert value(client, "lab_running_attempts") == 0
    time.sleep(0.2)  # the counter moves just after the commit that empties the gauge


def test_lost_leases_are_counted(client):
    portal, agent, job_id = isolated(client)
    _wait_until_no_attempt_is_active(client)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    before = value(client, "lab_lease_lost_total")
    _expire(attempt["attempt_id"])
    deadline = time.time() + 15
    while time.time() < deadline and value(client, "lab_lease_lost_total") == before:
        time.sleep(0.5)
    assert value(client, "lab_lease_lost_total") == before + 1


def _expire(attempt_id: str) -> None:
    import asyncio
    import concurrent.futures

    async def go():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with AsyncSession(engine) as session:
                await session.execute(
                    update(JobAttempt)
                    .where(JobAttempt.id == uuid.UUID(attempt_id))
                    .values(observed_state="unknown", lease_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
                )
                await session.commit()
        finally:
            await engine.dispose()

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        pool.submit(asyncio.run, go()).result()


def test_a_finished_image_pull_lands_in_the_pull_histogram(client):
    portal, agent, _job_id = isolated(client)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    before = value(client, "lab_image_pull_seconds_count")
    agent.renew(attempt, "starting", stage={"name": "pulling_image", "state": "started"})
    assert value(client, "lab_image_pull_seconds_count") == before
    time.sleep(0.05)
    agent.renew(attempt, "starting", stage={"name": "pulling_image", "state": "finished"})
    agent.renew(attempt, "starting", stage={"name": "pulling_image", "state": "finished"})  # a repeat is not a second pull
    assert value(client, "lab_image_pull_seconds_count") == before + 1
    assert value(client, "lab_image_pull_seconds_sum") > 0


async def test_metrics_wait_for_the_commit_and_vanish_on_rollback(client):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        before = value(client, "lab_jobs_finished_total", outcome="failed")
        async with AsyncSession(engine) as session:
            await session.execute(text("select 1"))
            metrics.job_finished(session, "failed")
            assert value(client, "lab_jobs_finished_total", outcome="failed") == before
            await session.rollback()
            await session.commit()
        assert value(client, "lab_jobs_finished_total", outcome="failed") == before
        async with AsyncSession(engine) as session:
            await session.execute(text("select 1"))
            metrics.job_finished(session, "failed")
            assert value(client, "lab_jobs_finished_total", outcome="failed") == before
            await session.commit()
        assert value(client, "lab_jobs_finished_total", outcome="failed") == before + 1
    finally:
        await engine.dispose()


# --- Grafana dashboard ----------------------------------------------------------

DASHBOARD = ROOT / "deploy" / "grafana" / "dashboards" / "lab-overview.json"
METRIC_NAME = re.compile(r"\blab_[a-z0-9_]+")


def _panels(dashboard: dict) -> list[dict]:
    found = []
    for panel in dashboard.get("panels", []):
        found.extend(_panels(panel) if panel.get("type") == "row" else [panel])
        found.extend(_panels({"panels": panel.get("panels", [])}))
    return found


def test_every_dashboard_query_names_a_metric_the_endpoint_serves(client):
    served = served_names(scrape_text(client))
    dashboard = json.loads(DASHBOARD.read_text())
    panels = _panels(dashboard)
    assert panels
    used = set()
    for panel in panels:
        targets = panel.get("targets") or []
        assert targets, f"panel {panel['title']!r} has no query"
        for target in targets:
            names = METRIC_NAME.findall(target["expr"])
            assert names, f"panel {panel['title']!r} query names no lab_ metric: {target['expr']}"
            for name in names:
                assert name in served, f"panel {panel['title']!r} uses {name}, which /metrics does not serve"
            used.update(names)
    # the specification's panels, by what they read
    for needed in (
        "lab_queue_jobs",
        "lab_nodes",
        "lab_running_attempts",
        "lab_time_to_start_seconds_bucket",
        "lab_lease_lost_total",
        "lab_jobs_finished_total",
        "lab_image_pull_seconds_bucket",
    ):
        assert needed in used, f"no panel reads {needed}"


def test_dashboard_panels_use_the_provisioned_datasource_and_have_unique_ids():
    dashboard = json.loads(DASHBOARD.read_text())
    source = yaml.safe_load((ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml").read_text())
    uid = source["datasources"][0]["uid"]
    panels = _panels(dashboard)
    assert len({p["id"] for p in panels}) == len(panels)
    for panel in panels:
        assert panel["datasource"]["uid"] == uid
        assert all(t["datasource"]["uid"] == uid for t in panel["targets"])
        pos = panel["gridPos"]
        assert 0 <= pos["x"] and pos["x"] + pos["w"] <= 24


def test_prometheus_scrapes_the_control_plane_metrics_path_with_the_token_file():
    config = yaml.safe_load((ROOT / "deploy/prometheus/prometheus.yml").read_text())
    (job,) = config["scrape_configs"]
    assert job["metrics_path"] == "/metrics"
    assert job["static_configs"][0]["targets"] == ["control:8080"]
    compose = (ROOT / "deploy/docker-compose.prod.yml").read_text()
    assert job["authorization"]["credentials_file"] == "/tmp/metrics-token"
    assert "/tmp/metrics-token" in compose and "retention.time=30d" in compose
