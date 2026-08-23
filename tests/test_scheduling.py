from __future__ import annotations

import uuid

from app.config import settings
from harness.client import FakeAgent
from harness.jobs import Portal


def _caps(tag: str, vram: int) -> dict:
    return {
        "compatibility_class": tag,
        "gpu_model": tag,
        "vram_total_mb": vram,
        "vram_allocatable_mb": vram,
    }


def test_explain_missing_class(client):
    portal = Portal(client)
    job = portal.submit_job(gpu_class=f"missing-{uuid.uuid4().hex[:8]}")
    got = portal.get_job(job["id"])
    assert got["status"] == "queued"
    assert got["pending_reason"].startswith("waiting for node class ")


def test_small_job_prefers_12g_over_reserved_24g(client):
    tag = f"lab-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=8000)
    small = FakeAgent(client, hostname=f"12g-{tag}", caps=_caps(tag, 12000))
    big = FakeAgent(client, hostname=f"24g-{tag}", caps=_caps(tag, 24576))
    small.register()
    small.heartbeat()
    big.register()
    big.heartbeat()
    assert big.claim() is None
    attempt = small.claim()
    assert attempt is not None
    assert attempt["job_id"] == job["id"]
    small.run_to_complete(attempt)
    assert portal.get_job(job["id"])["status"] == "completed"


def test_aging_steal_allows_24g_when_no_large_job(client, monkeypatch):
    monkeypatch.setattr(settings, "aging_steal_seconds", 0)
    tag = f"lab-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=8000)
    big = FakeAgent(client, hostname=f"24g-{tag}", caps=_caps(tag, 24576))
    big.register()
    big.heartbeat()
    attempt = big.claim()
    assert attempt is not None
    assert attempt["job_id"] == job["id"]
    big.run_to_complete(attempt)
    assert portal.get_job(job["id"])["status"] == "completed"


def test_large_job_keeps_24g_reserved(client, monkeypatch):
    monkeypatch.setattr(settings, "aging_steal_seconds", 0)
    tag = f"lab-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    portal.submit_job(gpu_class=tag, min_vram_mb=18000)
    small = portal.submit_job(gpu_class=tag, min_vram_mb=8000)
    big = FakeAgent(client, hostname=f"24g-{tag}", caps=_caps(tag, 24576))
    big.register()
    big.heartbeat()
    attempt = big.claim()
    assert attempt is not None
    assert attempt["job_id"] != small["id"]
    big.run_to_complete(attempt)
