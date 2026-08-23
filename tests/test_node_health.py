from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

from tests.test_scheduler_races import isolated


def test_probe_emits_class_and_hostname():
    probe = Path(__file__).resolve().parents[1] / "inventory" / "probe.py"
    result = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, check=True)
    record = json.loads(result.stdout.splitlines()[-1])
    assert record["hostname"]
    assert record["compatibility_class"]
    assert record["recommended_eligibility"] in {"eligible", "quarantined"}


def test_cuda_preflight_failure_quarantines(client):
    tag = uuid.uuid4().hex[:8]
    hostname = f"bad-cuda-{tag}"
    resp = client.post(
        "/agent/register",
        json={
            "hostname": hostname,
            "gpu_model": "NVIDIA GeForce RTX 4070",
            "compatibility_class": "ada-12g",
            "vram_total_mb": 12282,
            "vram_allocatable_mb": 12000,
            "compute_capability": "8.9",
            "driver_version": "test",
            "windows_version": "test",
            "wsl_version": "test",
            "agent_version": "test",
            "cpu_cores": 8,
            "ram_mb": 16000,
            "disk_free_bytes": 200_000_000_000,
            "cuda_preflight_ok": False,
        },
    )
    assert resp.status_code == 200, resp.text
    from harness.jobs import Portal

    portal = Portal(client)
    nodes = client.get("/nodes", headers=portal.headers)
    assert nodes.status_code == 200, nodes.text
    found = next(n for n in nodes.json()["nodes"] if n["hostname"] == hostname)
    assert found["eligibility"] == "quarantined"
    assert found["eligibility_reason"] == "cuda preflight failed"


def test_unhealthy_heartbeat_skips_claim(client):
    portal, agent, _job_id = isolated(client)
    resp = client.post(
        "/agent/heartbeat",
        json={
            "disk_linux_free_bytes": 200_000_000_000,
            "disk_windows_free_bytes": 200_000_000_000,
            "gpu_desktop_busy": False,
            "agent_healthy": False,
        },
        headers=agent._headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["command"] == "quarantine"
    assert agent.claim() is None
