from __future__ import annotations

import os
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.models import Node
from app.scheduler import claim
from harness.client import FakeAgent
from harness.jobs import Portal

OLD_DIGEST = "10.0.0.9:5001/lab-env/old@sha256:" + ("cd" * 32)


def _caps(tag: str, vram: int = 8000, cc: str | None = None) -> dict:
    caps = {
        "compatibility_class": tag,
        "gpu_model": tag,
        "vram_total_mb": vram,
        "vram_allocatable_mb": vram,
    }
    if cc:
        caps["compute_capability"] = cc
    return caps


def _agent(client, tag: str, name: str, **kw) -> FakeAgent:
    agent = FakeAgent(client, hostname=f"{name}-{tag}", caps=_caps(tag, **kw))
    agent.register()
    agent.heartbeat()
    return agent


async def test_concurrent_claims_take_different_jobs(client):
    # One node's open claim transaction must not hide the rest of the queue.
    tag = f"cc-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    jobs = {portal.submit_job(gpu_class=tag)["id"], portal.submit_job(gpu_class=tag)["id"]}
    a = _agent(client, tag, "a")
    b = _agent(client, tag, "b")
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as s1, AsyncSession(engine) as s2:
            first = await claim(s1, await s1.get(Node, uuid.UUID(a.node_id)))
            second = await claim(s2, await s2.get(Node, uuid.UUID(b.node_id)))
            assert first is not None and second is not None
            assert {str(first.job_id), str(second.job_id)} == jobs
            await s1.rollback()
            await s2.rollback()
    finally:
        await engine.dispose()


def test_disk_request_is_matched(client):
    tag = f"disk-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    # Fake nodes report 200 GB free; ask for more than that.
    job = portal.submit_job(gpu_class=tag, disk_mb=400_000)
    agent = _agent(client, tag, "n")
    assert agent.claim() is None
    assert portal.get_job(job["id"])["pending_reason"] == "waiting for disk on eligible nodes"
    fits = portal.submit_job(gpu_class=tag, disk_mb=10_000)
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == fits["id"]
    agent.run_to_complete(attempt)


def test_gpu_job_needs_supported_arch(client):
    # The catalog PyTorch image lists 8.6 / 8.9; a 7.5 card is not supported.
    tag = f"arch-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=4000)
    turing = _agent(client, tag, "turing", cc="7.5")
    assert turing.claim() is None
    assert portal.get_job(job["id"])["pending_reason"] == "waiting for a GPU this environment supports"
    ada = _agent(client, tag, "ada", cc="8.9")
    attempt = ada.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    ada.run_to_complete(attempt)


def test_cpu_job_ignores_arch(client):
    tag = f"archcpu-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    agent = _agent(client, tag, "turing", cc="7.5")
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    agent.run_to_complete(attempt)


def test_server_enforces_max_runtime(client, monkeypatch):
    monkeypatch.setattr(settings, "runtime_grace_seconds", 0)
    tag = f"rt-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, max_runtime_seconds=1)
    agent = _agent(client, tag, "n")
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    assert agent.renew(attempt)["body"]["desired_state"] == "run"
    time.sleep(1.5)
    renewed = agent.renew(attempt)
    assert renewed["status_code"] == 200, renewed
    assert renewed["body"]["desired_state"] == "cancel"
    # The agent stops the container and reports it as cancelled.
    done = agent.complete(attempt, exit_code=143, failure_category="cancelled")
    assert done["status_code"] == 200, done
    got = portal.get_job(job["id"])
    assert got["status"] == "failed"
    assert got["latest_attempt"]["failure_category"] == "timeout"


def test_idempotency_key_returns_same_job(client):
    portal = Portal(client)
    key = f"submit-{uuid.uuid4().hex}"
    first = portal.submit_job(idempotency_key=key)
    again = portal.submit_job(idempotency_key=key)
    assert again["id"] == first["id"]
    other_user = Portal(client)
    assert other_user.submit_job(idempotency_key=key)["id"] != first["id"]
    assert portal.submit_job()["id"] != first["id"]


def test_claim_rebuilds_environment_on_old_registry(client):
    # A job queued before the registry moved must not be handed an old digest.
    tag = f"reg-{uuid.uuid4().hex[:8]}"
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag), role="build")
    builder.register()
    builder.heartbeat()
    build = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={"ok": True, "image_digest": OLD_DIGEST, "lockfile": req, "log": "old"},
        headers=builder._headers(),
    )
    assert portal.get_job(job["id"])["status"] == "queued"
    trainer = _agent(client, tag, "t")
    assert trainer.claim() is None
    assert portal.get_job(job["id"])["status"] == "building"
    rebuild = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    assert rebuild["build_id"] == build["build_id"]
