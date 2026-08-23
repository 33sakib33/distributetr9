from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from harness.client import FakeAgent, stale_token
from harness.jobs import Portal


def isolated(client, name: str | None = None) -> tuple[Portal, FakeAgent, str]:
    tag = uuid.uuid4().hex[:8]
    caps = {
        "compatibility_class": f"iso-{tag}",
        "gpu_model": f"iso-{tag}",
        "vram_total_mb": 8192,
        "vram_allocatable_mb": 8000,
    }
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"iso-{tag}")
    agent = FakeAgent(client, hostname=name or f"node-{tag}", caps=caps)
    agent.register()
    agent.heartbeat()
    return portal, agent, job["id"]


def test_one_accepted_result(client):
    tag = uuid.uuid4().hex[:8]
    cls = f"iso-{tag}"
    caps = {
        "compatibility_class": cls,
        "gpu_model": cls,
        "vram_total_mb": 8192,
        "vram_allocatable_mb": 8000,
    }
    portal = Portal(client)
    job = portal.submit_job(gpu_class=cls)
    a = FakeAgent(client, hostname=f"a-{tag}", caps=caps)
    b = FakeAgent(client, hostname=f"b-{tag}", caps=caps)
    a.register()
    a.heartbeat()
    b.register()
    b.heartbeat()
    claim_a = a.claim()
    claim_b = b.claim()
    claimed = [c for c in (claim_a, claim_b) if c]
    assert len(claimed) == 1
    attempt = claimed[0]
    winner = a if claim_a else b
    winner.run_to_complete(attempt)
    got = portal.get_job(job["id"])
    assert got["status"] == "completed"
    assert got["accepted_attempt_id"] == attempt["attempt_id"]


def test_one_job_per_node(client):
    tag = uuid.uuid4().hex[:8]
    cls = f"iso-{tag}"
    caps = {
        "compatibility_class": cls,
        "gpu_model": cls,
        "vram_total_mb": 8192,
        "vram_allocatable_mb": 8000,
    }
    portal = Portal(client)
    portal.submit_job(gpu_class=cls)
    portal.submit_job(gpu_class=cls)
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=caps)
    agent.register()
    agent.heartbeat()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = [pool.submit(agent.claim).result(), pool.submit(agent.claim).result()]
    ids = {c["attempt_id"] for c in claims if c}
    assert len(ids) == 1
    agent.run_to_complete(next(c for c in claims if c))


def test_stale_token_rejected(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    assert attempt["job_id"] == job_id
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    bad = agent.complete(attempt, fencing_token=stale_token())
    assert bad["status_code"] == 409
    assert bad["body"]["error"] == "fenced"
    good = agent.complete(attempt)
    assert good["status_code"] == 200
    got = portal.get_job(job_id)
    assert got["status"] == "completed"
    assert got["accepted_attempt_id"] == attempt["attempt_id"]


def test_cancel_vs_complete(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_res = pool.submit(portal.cancel, job_id).result()
        complete_res = pool.submit(agent.complete, attempt).result()

    got = portal.get_job(job_id)
    assert got["status"] in {"cancelled", "completed"}
    if got["status"] == "completed":
        assert cancel_res["status_code"] in {200, 409}
        assert complete_res["status_code"] == 200
    else:
        assert complete_res["status_code"] in {200, 409}
        assert got["desired_state"] == "cancel"


def test_lease_expiry_retry(client):
    portal, agent, job_id = isolated(client)
    first = agent.claim()
    assert first is not None
    agent.acknowledge(first)
    agent.event(first, "running")
    deadline = time.time() + 12
    status = None
    while time.time() < deadline:
        time.sleep(0.5)
        status = portal.get_job(job_id)
        latest = status.get("latest_attempt") or {}
        if latest.get("failure_category") == "lease_lost" or (
            status["status"] == "queued" and latest.get("observed_state") == "failed"
        ):
            break
    assert status is not None
    latest = status["latest_attempt"]
    assert latest is not None
    assert latest["failure_category"] == "lease_lost"
    agent.heartbeat()
    second = agent.claim()
    assert second is not None
    assert second["attempt_id"] != first["attempt_id"]
    agent.run_to_complete(second)
    got = portal.get_job(job_id)
    assert got["status"] == "completed"
    assert got["accepted_attempt_id"] == second["attempt_id"]


def test_boot_session_fences_old_attempt(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    old_boot = agent.boot_session_id
    agent.register(reuse=True)
    assert agent.boot_session_id != old_boot
    fenced = agent.complete(attempt)
    assert fenced["status_code"] == 409
    got = portal.get_job(job_id)
    assert got["status"] != "completed"


def test_complete_idempotent(client):
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    first = agent.run_to_complete(attempt)
    second = agent.complete(attempt)
    assert first["status_code"] == 200
    assert second["status_code"] == 200
    assert second["body"]["attempt_id"] == first["body"]["attempt_id"]
    conflict = agent.complete(attempt, exit_code=1)
    assert conflict["status_code"] == 409
    got = portal.get_job(job_id)
    assert got["status"] == "completed"
    assert got["accepted_attempt_id"] == attempt["attempt_id"]
