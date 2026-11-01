from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from agent import updater
from app import agent_bundle
from app.config import settings
from harness.client import FakeAgent

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _bundle_headers(bundle: agent_bundle.Bundle) -> dict:
    return {
        "X-Agent-Version": bundle.version,
        "X-Bundle-Sha256": bundle.sha256,
        "X-Bundle-Signature": bundle.signature,
    }


def _install(root: Path, version: str, run_sh: str = "exit 0\n") -> None:
    release = root / "releases" / version
    release.mkdir(parents=True)
    (release / "agent.py").write_text(f'AGENT_VERSION = "{version}"\n')
    (release / "run.sh").write_text(run_sh)
    (root / "current").symlink_to(Path("releases") / version)


def test_signature_verifies_with_stdlib_code():
    bundle = agent_bundle.current_bundle()
    key = agent_bundle.public_key()
    assert updater.verify_signature(bundle.data, bundle.signature, key)
    assert not updater.verify_signature(bundle.data + b"x", bundle.signature, key)
    other = dict(key, n=format(int(key["n"], 16) - 2, "x"))
    assert not updater.verify_signature(bundle.data, bundle.signature, other)
    assert not updater.verify_signature(bundle.data, "00" * 10, key)


def test_bundle_is_deterministic():
    bundle = agent_bundle.current_bundle()
    assert agent_bundle._pack(agent_bundle.agent_dir()) == bundle.data


def test_apply_update_switches_release_and_confirms(tmp_path):
    bundle = agent_bundle.current_bundle()
    _install(tmp_path, "0.0.1")
    update = agent_bundle.update_for("0.0.1", 0)
    assert update and update["version"] == bundle.version

    installed = updater.apply_update(
        lambda: (bundle.data, _bundle_headers(bundle)), update, agent_bundle.public_key(), tmp_path, "0.0.1"
    )
    assert installed == bundle.version
    assert os.readlink(tmp_path / "current") == f"releases/{bundle.version}"
    assert (tmp_path / "current" / "agent.py").exists()
    pending = json.loads((tmp_path / "pending").read_text())
    assert pending == {"version": bundle.version, "previous": "0.0.1"}
    assert updater.release_root(tmp_path / "releases" / bundle.version) == tmp_path

    assert not updater.confirm_running(tmp_path, "0.0.1")
    assert updater.confirm_running(tmp_path, bundle.version)
    assert not (tmp_path / "pending").exists()


def test_tampered_or_rejected_bundle_is_refused(tmp_path):
    bundle = agent_bundle.current_bundle()
    _install(tmp_path, "0.0.1")
    update = agent_bundle.update_for("0.0.1", 0)
    key = agent_bundle.public_key()
    bad_sig = dict(_bundle_headers(bundle), **{"X-Bundle-Signature": "ab" * 384})
    with pytest.raises(updater.UpdateError, match="signature"):
        updater.apply_update(lambda: (bundle.data, bad_sig), update, key, tmp_path, "0.0.1")
    with pytest.raises(updater.UpdateError, match="checksum"):
        updater.apply_update(lambda: (bundle.data + b"x", _bundle_headers(bundle)), update, key, tmp_path, "0.0.1")
    (tmp_path / "rejected").write_text(bundle.version + "\n")
    with pytest.raises(updater.UpdateError, match="rolled back"):
        updater.apply_update(lambda: (bundle.data, _bundle_headers(bundle)), update, key, tmp_path, "0.0.1")
    assert os.readlink(tmp_path / "current") == "releases/0.0.1"


def test_heartbeat_advertises_update_by_ring(client, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    agent = FakeAgent(client, hostname=f"old-{tag}", caps={
        "compatibility_class": "cpu", "gpu_model": "none",
        "vram_total_mb": 0, "vram_allocatable_mb": 0, "agent_version": "0.0.1",
    })
    reg = agent.register()
    assert reg["bundle_public_key"] == agent_bundle.public_key()
    update = agent.heartbeat()["agent_update"]
    assert update["version"] == agent_bundle.current_bundle().version

    resp = client.get("/agent/bundle", headers=agent._headers())
    assert resp.status_code == 200
    assert updater.verify_signature(resp.content, resp.headers["x-bundle-signature"], reg["bundle_public_key"])

    # Canary-only rollout: ring 2 nodes wait, ring 0 nodes get it.
    monkeypatch.setattr(settings, "agent_rollout_ring", 0)
    assert agent.heartbeat()["agent_update"] is None
    assert client.post(f"/nodes/{agent.node_id}/ring", json={"ring": 0}, headers=ADMIN).status_code == 200
    assert agent.heartbeat()["agent_update"] is not None

    harness = FakeAgent(client, hostname=f"h-{tag}")
    harness.register()
    assert harness.heartbeat()["agent_update"] is None


def test_outdated_agent_gets_no_work(client, monkeypatch):
    from harness.jobs import Portal

    monkeypatch.setattr(settings, "agent_min_version", "0.5.0")
    tag = f"min-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    caps = {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}
    old = FakeAgent(client, hostname=f"old-{tag}", caps=dict(caps, agent_version="0.4.9"))
    old.register()
    old.heartbeat()
    assert old.claim() is None
    new = FakeAgent(client, hostname=f"new-{tag}", caps=dict(caps, agent_version="0.5.0"))
    new.register()
    new.heartbeat()
    attempt = new.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    new.run_to_complete(attempt)


def test_launcher_rolls_back_a_crashing_release(tmp_path):
    launcher = Path(__file__).resolve().parents[1] / "agent" / "launcher.sh"
    _install(tmp_path, "0.6.0", run_sh="sleep 30\n")
    broken = tmp_path / "releases" / "0.7.0"
    broken.mkdir(parents=True)
    (broken / "run.sh").write_text("exit 1\n")
    os.replace(tmp_path / "current", tmp_path / "old-current")
    (tmp_path / "current").symlink_to("releases/0.7.0")
    (tmp_path / "old-current").unlink()
    (tmp_path / "pending").write_text(json.dumps({"version": "0.7.0", "previous": "0.6.0"}))

    env = dict(os.environ, LAB_AGENT_ROOT=str(tmp_path), LAB_AGENT_RESTART_DELAY="0")
    proc = subprocess.Popen(["bash", str(launcher)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 15
        while time.time() < deadline and os.readlink(tmp_path / "current") != "releases/0.6.0":
            time.sleep(0.2)
        assert os.readlink(tmp_path / "current") == "releases/0.6.0"
        assert (tmp_path / "rejected").read_text().split() == ["0.7.0"]
        assert not (tmp_path / "pending").exists()
    finally:
        proc.kill()
        proc.wait()


def test_install_package_has_everything_a_blank_pc_needs(client):
    import gzip as gz
    import io as stdio
    import tarfile as tf

    resp = client.get("/install/lab-node.tar.gz")
    assert resp.status_code == 200
    with tf.open(fileobj=stdio.BytesIO(gz.decompress(resp.content))) as archive:
        names = set(archive.getnames())
        installer = archive.extractfile("scripts/setup-lab-node.sh").read()
    for needed in ("agent/agent.py", "agent/updater.py", "agent/launcher.sh", "agent/run.sh",
                   "scripts/setup-lab-node.sh", "supervisor/bootstrap.ps1",
                   "supervisor/install.ps1", "supervisor/run-wsl-agent.ps1"):
        assert needed in names, needed
    assert b"\r\n" not in installer


def test_bootstrap_script_carries_this_servers_address(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://10.0.0.5:8080/")
    monkeypatch.setattr(settings, "registry_url", "10.0.0.5:5001")
    body = client.get("/install/bootstrap.ps1").text
    assert '[string]$Api = "http://10.0.0.5:8080"' in body
    assert '[string]$Registry = "10.0.0.5:5001"' in body
    assert "__LAB_" not in body.split("param(", 1)[1].split(")", 1)[0]
