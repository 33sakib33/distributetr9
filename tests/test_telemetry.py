"""Job usage, job stages and node health (B6-B9): the agent's samplers and pull
parser are tested with tools faked out; the server side through the API."""
from __future__ import annotations

import io
import os
import sys
import tarfile
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agent import agent as agent_mod
from agent import classify, executor
from agent.executor import PullProgress
from app.models import AttemptUsage, JobAttempt, NodeHealth
from app.telemetry import indices_to_keep, prune_health, thin_usage
from harness.client import FakeAgent
from harness.jobs import Portal
from tests.test_envbuild import _caps
from tests.test_scheduler_races import isolated

# --- agent: samplers --------------------------------------------------------


def test_nvidia_usage_takes_the_busiest_gpu_and_sums_memory():
    out = "35, 1200, 61\n80, 3000, 70\n"
    assert classify.parse_nvidia_usage(out) == {"gpu_pct": 80.0, "vram_mb": 4200.0, "gpu_temp_c": 70.0}


def test_nvidia_usage_tolerates_na_and_empty_output():
    assert classify.parse_nvidia_usage("[N/A], [N/A], [N/A]")["gpu_pct"] is None
    assert classify.parse_nvidia_usage(None) == {"gpu_pct": None, "vram_mb": None, "gpu_temp_c": None}


@pytest.mark.parametrize(
    ("raw", "mb"),
    [("12.5MiB / 1GiB", 12.5), ("2GiB / 4GiB", 2048.0), ("1048576B / 2GiB", 1.0), ("1024kB / 2GiB", 1024000 / 1024**2), ("--", None), ("", None)],
)
def test_docker_memory_units(raw, mb):
    got = classify.parse_mem_mb(raw)
    assert (got is None) if mb is None else got == pytest.approx(mb)


def test_docker_stats_line():
    row = '{"CPUPerc":"87.50%","MemUsage":"512MiB / 8GiB","Name":"lab-x"}'
    assert classify.parse_docker_stats(row) == {"cpu_pct": 87.5, "ram_mb": 512.0}
    assert classify.parse_docker_stats("not json") == {"cpu_pct": None, "ram_mb": None}


def test_missing_tools_give_null_fields_not_errors(monkeypatch):
    monkeypatch.setattr(classify, "_run", lambda *a, **k: None)
    assert classify.collect_usage("lab-x") == {"gpu_pct": None, "vram_mb": None, "cpu_pct": None, "ram_mb": None}
    health = classify.collect_health(check_docker=True)
    assert health["gpu_temp_c"] is None and health["docker_ok"] is False


def test_usage_collection_survives_a_crashing_tool(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("tool exploded")

    monkeypatch.setattr(classify, "_run", boom)
    assert classify.collect_usage("lab-x")["gpu_pct"] is None
    assert classify.collect_health(check_docker=False) == {}


def test_sampling_calls_use_short_timeouts(monkeypatch):
    timeouts = []

    def fake_run(cmd, timeout=8):
        timeouts.append(timeout)
        return None

    monkeypatch.setattr(classify, "_run", fake_run)
    classify.collect_usage("lab-x")
    classify.collect_health(check_docker=True)
    assert timeouts and max(timeouts) <= 5


def test_collected_usage_has_every_field(monkeypatch):
    def fake_run(cmd, timeout=8):
        if cmd[0] == "nvidia-smi":
            return "55, 2048, 66"
        return '{"CPUPerc":"10.00%","MemUsage":"256MiB / 1GiB"}'

    monkeypatch.setattr(classify, "_run", fake_run)
    assert classify.collect_usage("lab-x") == {"gpu_pct": 55.0, "vram_mb": 2048.0, "cpu_pct": 10.0, "ram_mb": 256.0}


# --- agent: docker pull progress ------------------------------------------------

PULL_OUTPUT = """3.12.7-slim: Pulling from library/python
a480a496ba95: Pulling fs layer
b11e9f0c2d11: Pulling fs layer
c0ffee000001: Already exists
a480a496ba95: Waiting
a480a496ba95: Verifying Checksum
a480a496ba95: Download complete
a480a496ba95: Pull complete
b11e9f0c2d11: Pull complete
Digest: sha256:abc
Status: Downloaded newer image for python:3.12.7-slim
"""


def test_pull_progress_counts_layers():
    tracker = PullProgress()
    seen = [tracker.feed(line) for line in PULL_OUTPUT.splitlines()]
    changes = [p for p in seen if p]
    assert changes[0] == {"done": 0, "total": 1, "unit": "layers"}
    assert changes[-1] == {"done": 3, "total": 3, "unit": "layers"}
    assert tracker.feed("Digest: sha256:abc") is None
    assert tracker.feed("deadbeef: Pulling from library/x") is None  # a tag, not a layer
    assert tracker.progress()["total"] == 3


def _fake_pull(monkeypatch, script: str, *, cached: bool) -> None:
    real_popen = executor.subprocess.Popen

    class Result:
        returncode = 0 if cached else 1

    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: Result())
    monkeypatch.setattr(
        executor.subprocess, "Popen", lambda cmd, **kw: real_popen([sys.executable, "-c", script], **kw)
    )


def test_pull_reports_stage_start_progress_and_finish(monkeypatch):
    script = "import sys\nsys.stdout.write(%r)" % PULL_OUTPUT
    _fake_pull(monkeypatch, script, cached=False)
    events = []
    assert executor._ensure_image("python:3.12.7-slim", on_stage=lambda *e: events.append(e)) is None
    assert events[0] == ("pulling_image", "started", None)
    assert events[-1] == ("pulling_image", "finished", {"done": 3, "total": 3, "unit": "layers"})
    assert ("pulling_image", "started", {"done": 1, "total": 3, "unit": "layers"}) in events


def test_cached_image_is_not_a_stage(monkeypatch):
    _fake_pull(monkeypatch, "print('should not run')", cached=True)
    events = []
    assert executor._ensure_image("python:3.12.7-slim", on_stage=lambda *e: events.append(e)) is None
    assert events == []


def test_failed_pull_returns_its_output_and_still_finishes_the_stage(monkeypatch):
    script = "import sys\nprint('Error response from daemon: manifest unknown')\nsys.exit(1)"
    _fake_pull(monkeypatch, script, cached=False)
    events = []
    err = executor._ensure_image("nope:1", on_stage=lambda *e: events.append(e))
    assert "manifest unknown" in err
    assert events[-1][:2] == ("pulling_image", "finished")


def test_a_pull_can_be_cancelled(monkeypatch):
    _fake_pull(monkeypatch, "import time\ntime.sleep(60)", cached=False)
    stop = threading.Event()
    stop.set()
    started = time.time()
    err = executor._ensure_image("big:1", stop=stop)
    assert err == "docker pull cancelled"
    assert time.time() - started < 10


# --- agent: queueing of stage events ---------------------------------------------


def test_progress_updates_replace_each_other_and_stage_changes_wake_the_renew_loop():
    tel = agent_mod.Telemetry(check_docker=False)
    tel.begin_job(None)
    tel.push_stage("pulling_image", "started", None)
    assert tel.wake.is_set()
    tel.wake.clear()
    tel.push_stage("pulling_image", "started", {"done": 1, "total": 4, "unit": "layers"})
    tel.push_stage("pulling_image", "started", {"done": 2, "total": 4, "unit": "layers"})
    assert not tel.wake.is_set()  # progress alone waits for the next renew
    tel.push_stage("pulling_image", "finished", {"done": 4, "total": 4, "unit": "layers"})
    assert tel.wake.is_set()
    _usage, stages = tel.take()
    assert [(s["name"], s["state"]) for s in stages] == [("pulling_image", "started"), ("pulling_image", "finished")]
    assert stages[0]["progress"] == {"done": 2, "total": 4, "unit": "layers"}
    tel.restore(stages)  # a renew that failed puts them back
    assert tel.take()[1] == stages


def test_sampling_only_runs_during_a_job(monkeypatch):
    tel = agent_mod.Telemetry(check_docker=False)
    tel.begin_job("lab-x")
    assert tel.sampling and tel.container == "lab-x"
    tel.usage = {"cpu_pct": 1.0}
    tel.end_job()
    assert not tel.sampling and tel.take()[0] is None


def _agent(tmp_path, **kwargs) -> agent_mod.Agent:
    agent = agent_mod.Agent("http://x", tmp_path / "state.json", sys.executable, "python", **kwargs)
    agent.state.update(node_token="t", node_id="n", boot_session_id="b", lease_ttl_seconds=3)
    return agent


def test_heartbeat_carries_health_only_when_known(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    sent = []
    monkeypatch.setattr(agent_mod, "http_json", lambda method, url, body=None, headers=None, **k: sent.append(body) or (200, {}))
    monkeypatch.setattr(agent_mod, "gpu_desktop_busy", lambda **k: False)
    agent.heartbeat()
    assert "health" not in sent[-1]
    agent.telemetry.health = {"gpu_temp_c": 51.0, "docker_ok": None}
    agent.heartbeat()
    assert sent[-1]["health"]["gpu_temp_c"] == 51.0


def _tar_gz(name: str, source: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = source.encode()
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_renew_runs_during_the_pull_and_reports_stages_and_usage(tmp_path, monkeypatch):
    """The renew thread is up before the image is pulled, so a long pull keeps
    the lease and its progress reaches the server."""
    agent = _agent(tmp_path, attempt_root=tmp_path / "work")
    calls = []

    def fake_http(method, url, body=None, headers=None, **k):
        calls.append((url, body))
        return 200, {"desired_state": "run"}

    monkeypatch.setattr(agent_mod, "http_json", fake_http)
    monkeypatch.setattr(agent_mod, "http_bytes", lambda *a, **k: (200, _tar_gz("train.py", "print(1)")))
    monkeypatch.setattr(agent_mod, "gpu_desktop_busy", lambda **k: False)
    monkeypatch.setattr(agent_mod, "collect_usage", lambda container: {"gpu_pct": None, "vram_mb": None, "cpu_pct": 3.0, "ram_mb": 100.0})
    monkeypatch.setattr(agent_mod, "USAGE_SAMPLE_SECONDS", 0.05)

    def renews() -> list[dict]:
        return [b for url, b in calls if url.endswith("/renew")]

    def fake_run(*, on_stage, stop, **kwargs):
        on_stage("pulling_image", "started", None)
        on_stage("pulling_image", "started", {"done": 2, "total": 5, "unit": "layers"})
        deadline = time.time() + 10
        while time.time() < deadline and not any(r.get("stages") for r in renews()):
            time.sleep(0.05)
        on_stage("pulling_image", "finished", {"done": 5, "total": 5, "unit": "layers"})
        on_stage("running", "started", None)
        deadline = time.time() + 10
        while time.time() < deadline and not (
            any(r.get("usage") for r in renews())
            and any(s["state"] == "finished" for r in renews() for s in r.get("stages", []))
        ):
            time.sleep(0.05)
        return 0, None

    monkeypatch.setattr(agent_mod, "run_local_python", fake_run)
    attempt = {
        "attempt_id": str(uuid.uuid4()),
        "job_id": str(uuid.uuid4()),
        "fencing_token": str(uuid.uuid4()),
        "entrypoint": "train.py",
        "args": [],
        "resources": {},
        "max_runtime_seconds": 60,
    }
    agent.execute(attempt)

    bodies = renews()
    staged = [s for b in bodies for s in b.get("stages", [])]
    assert staged[0]["name"] == "pulling_image" and staged[0]["progress"] == {"done": 2, "total": 5, "unit": "layers"}
    assert ("pulling_image", "finished") in {(s["name"], s["state"]) for s in staged}
    assert any(b.get("usage", {}).get("cpu_pct") == 3.0 for b in bodies)
    assert any(url.endswith("/complete") for url, _ in calls)


# --- server: usage ------------------------------------------------------------


def _running(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    return portal, agent, job_id, attempt


def _usage(client, portal, job_id) -> dict:
    resp = client.get(f"/jobs/{job_id}/usage", headers=portal.headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_renew_with_usage_stores_samples_and_peaks(client):
    portal, agent, job_id, attempt = _running(client)
    for gpu, vram, cpu, ram in [(40, 1000, 90.0, 500), (80, 3100, 95.5, 900), (60, 2000, 50.0, 700)]:
        out = agent.renew(attempt, usage={"gpu_pct": gpu, "vram_mb": vram, "cpu_pct": cpu, "ram_mb": ram})
        assert out["status_code"] == 200, out
    usage = _usage(client, portal, job_id)
    assert usage["attempt_id"] == attempt["attempt_id"]
    assert [s["vram_mb"] for s in usage["samples"]] == [1000, 3100, 2000]
    assert usage["samples"][1] == {**usage["samples"][1], "gpu_pct": 80.0, "cpu_pct": 95.5, "ram_mb": 900}
    assert usage["peak_vram_mb"] == 3100
    assert usage["peak_ram_mb"] == 900
    assert usage["avg_gpu_pct"] == pytest.approx(60.0)
    assert usage["vram_total_mb"] == 8192
    latest = portal.get_job(job_id)["latest_attempt"]
    assert (latest["peak_vram_mb"], latest["peak_ram_mb"]) == (3100, 900)
    assert latest["avg_gpu_pct"] == pytest.approx(60.0)


def test_cpu_only_samples_leave_gpu_peaks_null(client):
    portal, agent, job_id, attempt = _running(client)
    agent.renew(attempt, usage={"gpu_pct": None, "vram_mb": None, "cpu_pct": 12.0, "ram_mb": 300})
    usage = _usage(client, portal, job_id)
    assert usage["samples"][0]["gpu_pct"] is None
    assert usage["peak_vram_mb"] is None and usage["avg_gpu_pct"] is None
    assert usage["peak_ram_mb"] == 300


def test_a_sample_with_no_values_is_not_stored(client):
    portal, agent, job_id, attempt = _running(client)
    out = agent.renew(attempt, usage={"gpu_pct": None, "vram_mb": None, "cpu_pct": None, "ram_mb": None})
    assert out["status_code"] == 200
    assert _usage(client, portal, job_id)["samples"] == []


def test_renew_without_usage_and_the_log_counter_field_keep_working(client):
    portal, agent, job_id, attempt = _running(client)
    resp = client.post(
        f"/attempts/{attempt['attempt_id']}/renew",
        json={"fencing_token": attempt["fencing_token"], "observed_state": "running", "resource_usage": {"log_bytes": 5}},
        headers=agent._headers(),
    )
    assert resp.status_code == 200, resp.text
    assert _usage(client, portal, job_id)["samples"] == []


def test_usage_of_a_job_that_never_ran_is_empty(client):
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"never-{uuid.uuid4().hex[:6]}")
    usage = _usage(client, portal, job["id"])
    assert usage["attempt_id"] is None and usage["samples"] == []


def test_usage_and_node_detail_are_access_controlled(client, monkeypatch):
    from app.config import settings

    portal, agent, job_id, attempt = _running(client)
    agent.renew(attempt, usage={"cpu_pct": 1.0})
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    stranger = {"X-Dev-User": f"stranger-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get(f"/jobs/{job_id}/usage", headers=stranger).status_code == 403
    assert client.get(f"/nodes/{agent.node_id}", headers=stranger).status_code == 403
    owner = client.get(f"/jobs/{job_id}/usage", headers={"X-Dev-User": portal.email})
    assert owner.status_code == 200


# --- server: thinning, health pruning (direct database access) ----------------


def test_even_thinning_keeps_the_ends():
    assert indices_to_keep(5, 300) == [0, 1, 2, 3, 4]
    kept = indices_to_keep(1000, 300)
    assert len(kept) == 300 and kept[0] == 0 and kept[-1] == 999
    assert kept == sorted(set(kept))
    gaps = {b - a for a, b in zip(kept, kept[1:])}
    assert gaps <= {3, 4}


async def _with_session(fn):
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as session:
            result = await fn(session)
            await session.commit()
            return result
    finally:
        await engine.dispose()


async def _add_samples(attempt_id: str, count: int) -> None:
    base = datetime.now(timezone.utc) - timedelta(hours=1)

    async def add(session):
        session.add_all(
            AttemptUsage(
                attempt_id=uuid.UUID(attempt_id),
                at=base + timedelta(seconds=i),
                gpu_pct=float(i),
                vram_mb=i,
                cpu_pct=1.0,
                ram_mb=1,
            )
            for i in range(count)
        )

    await _with_session(add)


async def _sample_count(attempt_id: str) -> int:
    async def count(session):
        return await session.scalar(
            select(func.count()).select_from(AttemptUsage).where(AttemptUsage.attempt_id == uuid.UUID(attempt_id))
        )

    return await _with_session(count)


async def test_finished_attempts_keep_at_most_300_samples_and_running_ones_are_left_alone(client):
    portal, agent, job_id, attempt = _running(client)
    await _add_samples(attempt["attempt_id"], 650)
    assert await _with_session(thin_usage) == 0  # still running
    assert await _sample_count(attempt["attempt_id"]) == 650

    agent.event(attempt, "uploading")
    assert agent.complete(attempt)["status_code"] == 200
    assert await _with_session(thin_usage) == 350
    assert await _sample_count(attempt["attempt_id"]) == 300
    samples = _usage(client, portal, job_id)["samples"]
    assert samples[0]["vram_mb"] == 0 and samples[-1]["vram_mb"] == 649
    assert await _with_session(thin_usage) == 0


async def test_a_finished_attempt_under_the_limit_is_untouched(client):
    portal, agent, job_id, attempt = _running(client)
    await _add_samples(attempt["attempt_id"], 120)
    agent.event(attempt, "uploading")
    agent.complete(attempt)
    await _with_session(thin_usage)
    assert await _sample_count(attempt["attempt_id"]) == 120


# --- server: stages --------------------------------------------------------------


def _stage(name: str, state: str, progress: dict | None = None) -> dict:
    return {"name": name, "state": state, "progress": progress}


def test_renew_with_stage_events_builds_the_stage_map(client):
    portal, agent, job_id, attempt = _running(client)
    agent.renew(attempt, stage=_stage("pulling_image", "started", {"done": 1, "total": 4, "unit": "layers"}))
    mid = portal.get_job(job_id)["stages"]["pulling_image"]
    assert mid["finished_at"] is None
    assert mid["progress"] == {"done": 1, "total": 4, "unit": "layers"}

    agent.renew(
        attempt,
        stages=[
            _stage("pulling_image", "finished", {"done": 4, "total": 4, "unit": "layers"}),
            _stage("running", "started"),
        ],
    )
    stages = portal.get_job(job_id)["stages"]
    pull = stages["pulling_image"]
    assert pull["started_at"] == mid["started_at"]  # a later event keeps the first start
    assert pull["finished_at"] is not None
    assert pull["progress"]["done"] == 4
    assert stages["running"]["started_at"] and stages["running"]["finished_at"] is None
    assert stages["running"]["progress"] is None


def test_unknown_stage_names_and_bad_progress_are_ignored(client):
    portal, agent, job_id, attempt = _running(client)
    out = agent.renew(
        attempt,
        stages=[_stage("teleporting", "started"), _stage("running", "started", {"done": "x"})],
    )
    assert out["status_code"] == 200
    stages = portal.get_job(job_id)["stages"]
    assert "teleporting" not in stages
    assert stages["running"]["progress"] is None


def test_uploading_closes_running_and_completion_closes_uploading(client):
    portal, agent, job_id, attempt = _running(client)
    agent.renew(attempt, stages=[_stage("pulling_image", "started"), _stage("running", "started")])
    agent.event(attempt, "uploading")
    stages = portal.get_job(job_id)["stages"]
    assert stages["running"]["finished_at"] and stages["pulling_image"]["finished_at"]
    assert stages["uploading"]["started_at"] and stages["uploading"]["finished_at"] is None
    agent.complete(attempt)
    done = portal.get_job(job_id)
    assert done["stages"]["uploading"]["finished_at"] == done["latest_attempt"]["completed_at"]


def test_stages_of_the_latest_attempt_only(client):
    portal = Portal(client)
    tag = f"iso-{uuid.uuid4().hex[:8]}"
    caps = {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 8192, "vram_allocatable_mb": 8000}
    job = portal.submit_job(gpu_class=tag)
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=caps)
    agent.register()
    agent.heartbeat()
    first = agent.claim()
    agent.acknowledge(first)
    agent.renew(first, stage=_stage("running", "started"))
    agent.complete(first, exit_code=1, failure_category="node_lost")  # retryable: the job is queued again
    second = agent.claim()
    assert second["attempt_id"] != first["attempt_id"]
    assert portal.get_job(job["id"])["stages"] == {}


def test_building_environment_stage_comes_from_the_build_row(client):
    tag = f"envs-{uuid.uuid4().hex[:8]}"
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    waiting = portal.get_job(job["id"])
    assert waiting["status"] == "building"
    assert waiting["building_environment"] == {"state": "pending", "started_at": None}
    assert waiting["stages"]["building_environment"]["finished_at"] is None

    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    build = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    assert build["requirements"].strip() == req.strip()  # our build, not one another test left pending
    building = portal.get_job(job["id"])
    assert building["building_environment"]["state"] == "building"
    assert building["building_environment"]["started_at"]

    digest = "127.0.0.1:5000/lab-env/test@sha256:" + ("ab" * 32)
    client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={"ok": True, "image_digest": digest, "lockfile": req, "log": "ok"},
        headers=builder._headers(),
    )
    ready = portal.get_job(job["id"])
    assert ready["building_environment"] is None
    assert ready["stages"]["building_environment"]["finished_at"]


def test_job_without_a_custom_environment_has_no_building_stage(client):
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"plain-{uuid.uuid4().hex[:6]}")
    got = portal.get_job(job["id"])
    assert got["building_environment"] is None
    assert got["stages"] == {}


# --- server: node health ------------------------------------------------------------

HEALTH = {"gpu_temp_c": 61.0, "gpu_pct": 12.0, "vram_used_mb": 900, "driver_version": "551.1", "uptime_s": 3600, "docker_ok": True}


async def _health_rows(node_id: str) -> list[SimpleNamespace]:
    async def rows(session):
        found = await session.scalars(
            select(NodeHealth).where(NodeHealth.node_id == uuid.UUID(node_id)).order_by(NodeHealth.at)
        )
        return [
            SimpleNamespace(at=r.at, gpu_temp_c=r.gpu_temp_c, gpu_pct=r.gpu_pct, disk_free_bytes=r.disk_free_bytes, busy=r.busy)
            for r in found
        ]

    return await _with_session(rows)


async def _backdate_health(node_id: str, by: timedelta) -> None:
    async def move(session):
        await session.execute(
            update(NodeHealth).where(NodeHealth.node_id == uuid.UUID(node_id)).values(at=NodeHealth.at - by)
        )

    await _with_session(move)


async def test_health_samples_are_throttled_to_one_a_minute(client):
    agent = FakeAgent(client, hostname=f"hb-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat(health=HEALTH)
    agent.heartbeat(health={**HEALTH, "gpu_temp_c": 99.0})
    rows = await _health_rows(agent.node_id)
    assert len(rows) == 1
    assert (rows[0].gpu_temp_c, rows[0].gpu_pct) == (61.0, 12.0)
    assert rows[0].disk_free_bytes == 200_000_000_000 and rows[0].busy is False

    await _backdate_health(agent.node_id, timedelta(seconds=61))
    agent.heartbeat(health={**HEALTH, "gpu_temp_c": 70.0})
    rows = await _health_rows(agent.node_id)
    assert [r.gpu_temp_c for r in rows] == [61.0, 70.0]


async def test_heartbeat_without_health_still_leaves_a_sample(client):
    agent = FakeAgent(client, hostname=f"hb-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    rows = await _health_rows(agent.node_id)
    assert len(rows) == 1 and rows[0].gpu_temp_c is None


async def test_busy_follows_the_running_attempt(client):
    agent = FakeAgent(client, hostname=f"hb-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat(attempt_id=str(uuid.uuid4()))
    assert (await _health_rows(agent.node_id))[0].busy is True


async def test_health_older_than_a_week_is_pruned(client):
    agent = FakeAgent(client, hostname=f"hb-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat(health=HEALTH)
    await _backdate_health(agent.node_id, timedelta(days=8))
    agent.heartbeat(health=HEALTH)  # a fresh sample, not throttled: the old one is a week away
    assert len(await _health_rows(agent.node_id)) == 2

    async def prune(session):
        return await prune_health(session, datetime.now(timezone.utc))

    assert await _with_session(prune) >= 1
    rows = await _health_rows(agent.node_id)
    assert len(rows) == 1
    assert rows[0].at > datetime.now(timezone.utc) - timedelta(minutes=5)


def test_node_detail_has_health_history_attempts_and_restart(client):
    tag = f"iso-{uuid.uuid4().hex[:8]}"
    caps = {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 8192, "vram_allocatable_mb": 8000}
    portal = Portal(client)
    job_id = portal.submit_job(gpu_class=tag)["id"]
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=caps)
    agent.register(last_restart={"reason": "crash", "exit_code": 1, "detail": "boom", "at": "2026-11-01T20:30:00Z"})
    agent.heartbeat(health=HEALTH)
    attempt = agent.claim()
    agent.run_to_complete(attempt)

    resp = client.get(f"/nodes/{agent.node_id}", headers=portal.headers)
    assert resp.status_code == 200, resp.text
    node = resp.json()
    assert node["hostname"] == agent.hostname and node["id"] == agent.node_id
    assert node["last_restart"]["reason"] == "crash"
    assert node["last_health"]["health"]["driver_version"] == "551.1"
    assert node["health_history"][0]["gpu_temp_c"] == 61.0
    assert [a["job_id"] for a in node["recent_attempts"]] == [job_id]
    assert node["recent_attempts"][0]["state"] == "completed"
    assert node["events"][0]["kind"] == "restart" and node["events"][0]["payload"]["reason"] == "crash"


def test_node_detail_limits_attempts_and_events_to_twenty(client):
    portal, agent, _job = isolated(client)
    for _ in range(22):
        agent.register(last_restart={"reason": "manual"})
    node = client.get(f"/nodes/{agent.node_id}", headers=portal.headers).json()
    assert len(node["events"]) == 20


def test_unknown_node_is_404(client):
    portal = Portal(client)
    assert client.get(f"/nodes/{uuid.uuid4()}", headers=portal.headers).status_code == 404


async def test_attempt_rows_carry_the_peak_columns(client):
    portal, agent, job_id, attempt = _running(client)
    agent.renew(attempt, usage={"vram_mb": 2500.4, "ram_mb": 100.6})

    async def peaks(session):
        row = await session.get(JobAttempt, uuid.UUID(attempt["attempt_id"]))
        return row.peak_vram_mb, row.peak_ram_mb

    assert await _with_session(peaks) == (2500, 101)
