from __future__ import annotations

from app.config import settings
from harness.jobs import Portal
from tests.test_scheduler_races import isolated


def test_log_rate_limit_drops_with_warning(client, monkeypatch):
    monkeypatch.setattr(settings, "log_lines_per_second", 3)
    monkeypatch.setattr(settings, "log_chunk_lines", 500)
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    posted = agent.post_logs(attempt, [f"line-{i}" for i in range(10)])
    assert posted["status_code"] == 200, posted
    assert posted["body"].get("truncated") is True
    logs = client.get(f"/jobs/{job_id}/logs", headers=portal.headers)
    assert logs.status_code == 200, logs.text
    lines = [c["line"] for c in logs.json()["chunks"]]
    assert "log truncated: rate or size limit exceeded" in lines
    assert len(lines) <= 4


def test_log_byte_limit_drops_with_warning(client, monkeypatch):
    monkeypatch.setattr(settings, "log_bytes_per_attempt", 120)
    monkeypatch.setattr(settings, "log_lines_per_second", 200)
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    posted = agent.post_logs(attempt, ["x" * 80, "y" * 80, "z" * 80])
    assert posted["status_code"] == 200, posted
    logs = client.get(f"/jobs/{job_id}/logs", headers=portal.headers)
    lines = [c["line"] for c in logs.json()["chunks"]]
    assert "log truncated: rate or size limit exceeded" in lines


def test_output_file_limit_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "output_file_bytes", 8)
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    put = agent.put_output(attempt, "out.bin", b"0123456789")
    assert put["status_code"] == 413
    done = agent.complete(attempt, exit_code=0)
    assert done["status_code"] == 200
    got = portal.get_job(job_id)
    assert got["status"] == "completed"


def test_oversized_manifest_fails_resource_without_promote(client, monkeypatch):
    monkeypatch.setattr(settings, "output_bytes_per_attempt", 5)
    portal, agent, job_id = isolated(client)
    attempt = agent.claim()
    assert attempt is not None
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    resp = client.post(
        f"/attempts/{attempt['attempt_id']}/complete",
        json={
            "fencing_token": attempt["fencing_token"],
            "exit_code": 0,
            "output_manifest": [
                {
                    "relative_path": "out.bin",
                    "size_bytes": 100,
                    "checksum_sha256": "ab" * 32,
                }
            ],
        },
        headers=agent._headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    got = portal.get_job(job_id)
    assert got["status"] == "failed"
    arts = client.get(f"/jobs/{job_id}/artifacts", headers=portal.headers)
    types = {a["type"] for a in arts.json()["artifacts"]}
    assert "output" not in types
