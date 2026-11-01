from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from harness.client import FakeAgent
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}
ROOT = Path(__file__).resolve().parents[1]


def _caps(tag: str) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}


def test_revoked_node_cannot_call_back(client):
    tag = f"rev-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=_caps(tag))
    agent.register()
    agent.heartbeat()
    resp = client.post(f"/nodes/{agent.node_id}/revoke", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "credential revoked"
    assert client.post("/agent/claim", json={}, headers=agent._headers()).status_code == 401


def test_admin_alerts_endpoint(client, monkeypatch):
    from app.config import settings

    resp = client.get("/admin/alerts", headers=ADMIN)
    assert resp.status_code == 200
    assert isinstance(resp.json()["groups"], list)
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/alerts", headers=student).status_code == 403


def test_gc_removes_only_unaccepted_outputs(client):
    tag = f"gc-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=_caps(tag))
    agent.register()
    agent.heartbeat()

    lost = agent.claim()
    agent.acknowledge(lost)
    agent.event(lost, "running")
    assert agent.put_output(lost, "partial.txt", b"half")["status_code"] == 200
    # A reboot fences the attempt and the job is retried.
    agent.register()
    agent.heartbeat()
    won = agent.claim()
    assert won["attempt_id"] != lost["attempt_id"]
    agent.acknowledge(won)
    agent.event(won, "running")
    agent.put_output(won, "result.txt", b"done")
    agent.event(won, "uploading")
    resp = client.post(
        f"/attempts/{won['attempt_id']}/complete",
        json={
            "fencing_token": won["fencing_token"],
            "exit_code": 0,
            "output_manifest": [{"relative_path": "result.txt", "size_bytes": 4,
                                 "checksum_sha256": __import__("hashlib").sha256(b"done").hexdigest()}],
        },
        headers=agent._headers(),
    )
    assert resp.status_code == 200, resp.text

    env = dict(os.environ, PYTHONPATH=str(ROOT / "control"))
    out = subprocess.run(
        [sys.executable, "-m", "app.ops", "gc", "--days", "0"],
        cwd=ROOT / "control", env=env, capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stderr
    from app.storage import storage

    assert storage.list_keys(f"jobs/{job['id']}/attempts/{lost['attempt_id']}/") == []
    assert storage.list_keys(f"jobs/{job['id']}/promoted/") == [f"jobs/{job['id']}/promoted/result.txt"]


def test_release_returns_drained_node_to_service(client):
    tag = f"rel-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=_caps(tag))
    agent.register()
    agent.heartbeat()
    assert client.post(f"/nodes/{agent.node_id}/drain", headers=ADMIN).status_code == 200
    agent.register()  # a restart does not undo a drain
    agent.heartbeat()
    assert agent.claim() is None
    assert client.post(f"/nodes/{agent.node_id}/release", headers=ADMIN).json()["eligibility"] == "eligible"
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    agent.run_to_complete(attempt)

    client.post(f"/nodes/{agent.node_id}/revoke", headers=ADMIN)
    assert client.post(f"/nodes/{agent.node_id}/release", headers=ADMIN).status_code == 409
