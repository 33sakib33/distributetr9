from __future__ import annotations

import uuid

from app.config import settings
from app.envbuild import environment_hash, is_registry_digest, normalize_requirements
from harness.client import FakeAgent
from harness.jobs import Portal, make_archive

READY_DIGEST = "127.0.0.1:5000/lab-env/test@sha256:" + ("ab" * 32)


def _caps(tag: str, vram: int) -> dict:
    return {
        "compatibility_class": tag,
        "gpu_model": tag,
        "vram_total_mb": vram,
        "vram_allocatable_mb": vram,
    }


def test_normalize_and_hash():
    a = normalize_requirements("# comment\nNumPy==1.26.4\n\nnumpy==1.26.4\n")
    b = normalize_requirements("numpy==1.26.4")
    assert a == b == ["numpy==1.26.4"]
    h1 = environment_hash(a, base_image="pytorch/x", compat_class="ampere-8g")
    h2 = environment_hash(b, base_image="pytorch/x", compat_class="ampere-8g")
    assert h1 == h2
    h3 = environment_hash(a, base_image="pytorch/x", compat_class="cpu")
    assert h1 != h3


def test_registry_digest_rejects_local_ids():
    assert is_registry_digest(READY_DIGEST)
    assert is_registry_digest("pytorch/pytorch@sha256:" + ("cd" * 32))
    assert not is_registry_digest("sha256:testdigest")
    assert not is_registry_digest("sha256:" + ("ab" * 32))
    assert not is_registry_digest("lab-env:deadbeef")
    assert not is_registry_digest("")


def test_invalid_requirements_rejected(client):
    portal = Portal(client)
    project_id = portal.ensure_project()
    resp = client.post(
        "/jobs",
        data={
            "project_id": project_id,
            "entrypoint": "train.py",
            "environment_id": portal.environment_id(),
            "args": "[]",
            "resources": "{}",
            "requirements": "-r other.txt\n",
        },
        files={"archive": ("hello.tar.gz", make_archive(), "application/gzip")},
        headers=portal.headers,
    )
    assert resp.status_code == 400, resp.text


def test_requirements_rejected_without_registry(client, monkeypatch):
    monkeypatch.setattr(settings, "registry_url", "")
    portal = Portal(client)
    project_id = portal.ensure_project()
    resp = client.post(
        "/jobs",
        data={
            "project_id": project_id,
            "entrypoint": "train.py",
            "environment_id": portal.environment_id(),
            "args": "[]",
            "resources": "{}",
            "requirements": "numpy==1.26.4\n",
        },
        files={"archive": ("hello.tar.gz", make_archive(), "application/gzip")},
        headers=portal.headers,
    )
    assert resp.status_code == 400, resp.text
    assert "REGISTRY_URL" in resp.json()["message"]


def test_two_jobs_share_build_and_skip_gpu_until_ready(client):
    tag = f"envb-{uuid.uuid4().hex[:8]}"
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    portal = Portal(client)
    first = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    second = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    assert first["environment_id"] == second["environment_id"]
    assert first["status"] == "building"
    assert second["status"] == "building"
    assert first["pending_reason"] == "building environment"

    trainer = FakeAgent(client, hostname=f"t-{tag}", caps=_caps(tag, 8000), role="train")
    trainer.register()
    trainer.heartbeat()
    raw_train = client.post("/agent/claim", json={}, headers=trainer._headers())
    assert raw_train.status_code == 200, raw_train.text
    assert raw_train.json().get("attempt") is None
    assert raw_train.json().get("build") is None

    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    raw = client.post("/agent/claim", json={}, headers=builder._headers())
    assert raw.status_code == 200, raw.text
    build = raw.json().get("build")
    assert build and build.get("build_id")
    assert raw.json().get("attempt") is None
    assert "lab-env/" in (build.get("image_tag") or "")

    still = trainer.claim()
    assert still is None
    assert portal.get_job(first["id"])["status"] == "building"

    done = client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={
            "ok": True,
            "image_digest": READY_DIGEST,
            "lockfile": req,
            "log": "pip install ok",
        },
        headers=builder._headers(),
    )
    assert done.status_code == 200, done.text
    got = portal.get_job(first["id"])
    assert got["status"] == "queued"
    logs = client.get(f"/jobs/{first['id']}/build-logs", headers=portal.headers)
    assert logs.status_code == 200
    assert "pip install ok" in logs.json()["text"]
    assert "pip" not in str(client.get(f"/jobs/{first['id']}/logs", headers=portal.headers).json())
    attempt = trainer.claim()
    assert attempt is not None
    assert attempt["job_id"] in {first["id"], second["id"]}
    assert attempt["image_digest"] == READY_DIGEST
    trainer.run_to_complete(attempt)
    leftover = trainer.claim()
    assert leftover is not None
    trainer.run_to_complete(leftover)
    assert builder.claim() is None


def test_ready_build_on_old_registry_is_rebuilt(client):
    tag = f"envr-{uuid.uuid4().hex[:8]}"
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    old_digest = "10.0.0.9:5001/lab-env/old@sha256:" + ("cd" * 32)
    portal = Portal(client)
    first = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    build = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    done = client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={"ok": True, "image_digest": old_digest, "lockfile": req, "log": "old registry"},
        headers=builder._headers(),
    )
    assert done.json()["state"] == "ready"
    assert portal.get_job(first["id"])["status"] == "queued"

    # REGISTRY_URL is 127.0.0.1:5000 in tests, so the ready digest is unpullable.
    second = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    assert second["environment_id"] == first["environment_id"]
    assert second["status"] == "building"
    assert portal.get_job(first["id"])["status"] == "building"

    trainer = FakeAgent(client, hostname=f"t-{tag}", caps=_caps(tag, 8000), role="train")
    trainer.register()
    trainer.heartbeat()
    assert trainer.claim() is None

    rebuild = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    assert rebuild["build_id"] == build["build_id"]
    assert rebuild["image_tag"].startswith(settings.registry_url + "/lab-env/")
    client.post(
        f"/agent/builds/{rebuild['build_id']}/complete",
        json={"ok": True, "image_digest": READY_DIGEST, "lockfile": req, "log": "rebuilt"},
        headers=builder._headers(),
    )
    attempt = trainer.claim()
    assert attempt is not None
    assert attempt["image_digest"] == READY_DIGEST
    trainer.run_to_complete(attempt)
    trainer.run_to_complete(trainer.claim())


def test_local_image_id_does_not_queue(client):
    tag = f"envl-{uuid.uuid4().hex[:8]}"
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    raw = client.post("/agent/claim", json={}, headers=builder._headers())
    build = raw.json().get("build")
    assert build
    done = client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={
            "ok": True,
            "image_digest": "sha256:" + ("ab" * 32),
            "lockfile": req,
            "log": "local id only",
        },
        headers=builder._headers(),
    )
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "failed"
    got = portal.get_job(job["id"])
    assert got["status"] == "failed"
    assert got["pending_reason"] == "environment build failed"


def test_failed_build_never_queued(client):
    tag = f"envf-{uuid.uuid4().hex[:8]}"
    req = f"bad-{uuid.uuid4().hex[:10]}==9.9.9\n"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000, requirements=req)
    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    raw = client.post("/agent/claim", json={}, headers=builder._headers())
    build = raw.json().get("build")
    assert build
    fail = client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={"ok": False, "error": "import failed", "log": "pip failed for tests"},
        headers=builder._headers(),
    )
    assert fail.status_code == 200, fail.text
    got = portal.get_job(job["id"])
    assert got["status"] == "failed"
    assert got["pending_reason"] == "environment build failed"
    trainer = FakeAgent(client, hostname=f"t-{tag}", caps=_caps(tag, 8000), role="train")
    trainer.register()
    trainer.heartbeat()
    assert trainer.claim() is None


def test_catalog_environment_still_claims(client):
    tag = f"cat-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000)
    assert job["status"] == "queued"
    agent = FakeAgent(client, hostname=f"n-{tag}", caps=_caps(tag, 8000))
    agent.register()
    agent.heartbeat()
    attempt = agent.claim()
    assert attempt is not None
    assert attempt["job_id"] == job["id"]
    agent.run_to_complete(attempt)
    assert portal.get_job(job["id"])["status"] == "completed"


def test_builder_does_not_claim_catalog_jobs(client):
    tag = f"bcat-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag, min_vram_mb=1000)
    builder = FakeAgent(client, hostname=f"b-{tag}", caps=_caps(tag, 8000), role="build")
    builder.register()
    builder.heartbeat()
    raw = client.post("/agent/claim", json={}, headers=builder._headers())
    assert raw.status_code == 200, raw.text
    assert raw.json().get("attempt") is None
    assert raw.json().get("build") is None
    trainer = FakeAgent(client, hostname=f"t-{tag}", caps=_caps(tag, 8000), role="train")
    trainer.register()
    trainer.heartbeat()
    attempt = trainer.claim()
    assert attempt is not None
    assert attempt["job_id"] == job["id"]
    trainer.run_to_complete(attempt)
