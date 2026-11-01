"""Catalog images served from the lab registry: mirror builds (B2) and the
catalog rows they update (B4)."""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.catalog import PYTHON_CPU_IMAGE, PYTORCH_IMAGE, TENSORFLOW_IMAGE
from app.config import settings
from app.envbuild import ensure_mirror_builds, mirror_tag_for
from app.models import Environment, EnvironmentBuild
from app.seed import PYTHON_CPU_ID, TENSORFLOW_ID, seed
from harness.client import GPU_CLASSES, FakeAgent
from harness.jobs import Portal

REGISTRY = "127.0.0.1:5000"
EXPECTED_TAGS = {
    "pytorch-stable-cu124": f"{REGISTRY}/lab/pytorch-stable-cu124:2.5.1-cuda12.4-cudnn9-runtime",
    "python-cpu": f"{REGISTRY}/lab/python-cpu:3.12.7-slim",
    "tensorflow-stable": f"{REGISTRY}/lab/tensorflow-stable:2.17.0-gpu",
}


@asynccontextmanager
async def db():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as session:
            yield session
    finally:
        await engine.dispose()


def mirror_digest(name: str, byte: str = "ab") -> str:
    return f"{REGISTRY}/lab/{name}@sha256:" + byte * 32


@pytest.fixture
async def mirroring(monkeypatch):
    """Turn mirror builds on, and leave the shared test database as it was found:
    catalog rows back on their public images, no mirror build waiting for a builder."""
    monkeypatch.setattr(settings, "mirror_catalog_images", True)
    yield
    async with db() as session:
        await session.execute(
            text(
                "UPDATE environments SET image_digest = upstream_image, upstream_image = NULL "
                "WHERE upstream_image IS NOT NULL"
            )
        )
        await session.execute(
            text("DELETE FROM artifacts WHERE build_id IN (SELECT id FROM environment_builds WHERE kind = 'mirror')")
        )
        await session.execute(text("DELETE FROM environment_builds WHERE kind = 'mirror'"))
        await session.commit()


def _builder(client) -> FakeAgent:
    builder = FakeAgent(client, hostname=f"mirror-b-{uuid.uuid4().hex[:8]}", caps=GPU_CLASSES[0], role="build")
    builder.register()
    builder.heartbeat()
    return builder


def _claim_build(client, builder: FakeAgent) -> dict | None:
    resp = client.post("/agent/claim", json={}, headers=builder._headers())
    assert resp.status_code == 200, resp.text
    return resp.json().get("build")


def _complete(client, builder: FakeAgent, build: dict, **body) -> dict:
    resp = client.post(f"/agent/builds/{build['build_id']}/complete", json=body, headers=builder._headers())
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _envs() -> dict[str, Environment]:
    async with db() as session:
        rows = (await session.scalars(select(Environment).where(~Environment.name.like("build-%")))).all()
        return {e.name: e for e in rows}


def test_mirror_tag_is_a_valid_docker_tag():
    env = Environment(name="My Env_1")
    assert mirror_tag_for(env, "pytorch/pytorch:2.5.1-cuda12.4", registry_url="reg:5000/") == (
        "reg:5000/lab/my-env_1:2.5.1-cuda12.4"
    )
    assert mirror_tag_for(env, "ubuntu", registry_url="reg:5000").endswith(":latest")
    # A registry port is not a tag; a digest pin is not part of it either.
    assert mirror_tag_for(env, "localhost:5000/team/img", registry_url="r").endswith(":latest")
    assert mirror_tag_for(env, "x/y:1.0@sha256:" + "0" * 64, registry_url="r").endswith(":1.0")


async def test_mirror_build_per_catalog_env_rewrites_digest(client, mirroring):
    async with db() as session:
        assert await ensure_mirror_builds(session) == 3
        await session.commit()
    async with db() as session:
        assert await ensure_mirror_builds(session) == 0  # already queued: no duplicates

    builder = _builder(client)
    claimed = {}
    for _ in range(3):
        build = _claim_build(client, builder)
        assert build is not None and build["kind"] == "mirror"
        claimed[build["image_tag"]] = build
    assert _claim_build(client, builder) is None
    assert set(claimed) == set(EXPECTED_TAGS.values())
    assert claimed[EXPECTED_TAGS["python-cpu"]]["source_image"] == PYTHON_CPU_IMAGE
    assert claimed[EXPECTED_TAGS["pytorch-stable-cu124"]]["source_image"] == PYTORCH_IMAGE

    for name, tag in EXPECTED_TAGS.items():
        _complete(client, builder, claimed[tag], ok=True, image_digest=mirror_digest(name), log="mirrored")

    envs = await _envs()
    assert envs["pytorch-stable-cu124"].image_digest == mirror_digest("pytorch-stable-cu124")
    assert envs["pytorch-stable-cu124"].upstream_image == PYTORCH_IMAGE
    assert envs["tensorflow-stable"].upstream_image == TENSORFLOW_IMAGE
    assert envs["python-cpu"].upstream_image == PYTHON_CPU_IMAGE
    async with db() as session:
        assert await ensure_mirror_builds(session) == 0  # everything is served from the registry

    # Jobs on a catalog environment now receive the registry digest.
    gpu_class = f"mir-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=gpu_class, min_vram_mb=1000)
    trainer = FakeAgent(
        client,
        hostname=f"mirror-t-{gpu_class}",
        caps={
            "compatibility_class": gpu_class,
            "gpu_model": gpu_class,
            "vram_total_mb": 8192,
            "vram_allocatable_mb": 8000,
        },
    )
    trainer.register()
    trainer.heartbeat()
    attempt = trainer.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]
    assert attempt["image_digest"] == mirror_digest("pytorch-stable-cu124")
    trainer.run_to_complete(attempt)


async def test_failed_mirror_leaves_catalog_alone_and_retries_later(client, mirroring):
    async with db() as session:
        await ensure_mirror_builds(session)
        await session.commit()
    builder = _builder(client)
    build = _claim_build(client, builder)
    assert build is not None
    out = _complete(client, builder, build, ok=False, error="pull access denied", log="boom")
    assert out["state"] == "failed"

    envs = await _envs()
    assert all(e.upstream_image is None for e in envs.values())
    assert envs["tensorflow-stable"].image_digest == TENSORFLOW_IMAGE

    async with db() as session:
        failed = await session.scalar(select(EnvironmentBuild).where(EnvironmentBuild.id == uuid.UUID(build["build_id"])))
        assert failed.error_message == "pull access denied"
        assert await ensure_mirror_builds(session) == 0  # too soon to retry
        await session.execute(
            text("UPDATE environment_builds SET finished_at = now() - interval '1 hour' WHERE state = 'failed'")
        )
        await session.commit()
    async with db() as session:
        assert await ensure_mirror_builds(session) == 1
        await session.commit()
    seen = []
    while (again := _claim_build(client, builder)) is not None:
        seen.append(again["build_id"])
    assert build["build_id"] in seen


async def test_digest_outside_current_registry_is_rejected(client, mirroring):
    async with db() as session:
        await ensure_mirror_builds(session)
        await session.commit()
    builder = _builder(client)
    build = _claim_build(client, builder)
    assert build is not None
    out = _complete(client, builder, build, ok=True, image_digest="10.9.9.9:5000/lab/x@sha256:" + "ab" * 32, log="")
    assert out["state"] == "failed"
    envs = await _envs()
    assert all(e.upstream_image is None for e in envs.values())


async def test_registry_move_requeues_mirror_without_resetting_catalog(client, mirroring):
    old = "10.0.0.9:5001/lab/pytorch-stable-cu124@sha256:" + "cd" * 32
    async with db() as session:
        await session.execute(
            update(Environment)
            .where(Environment.name == "pytorch-stable-cu124")
            .values(image_digest=old, upstream_image=PYTORCH_IMAGE)
        )
        await session.commit()
    # Jobs on the catalog row stay runnable: nothing resets it behind their back.
    gpu_class = f"mov-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=gpu_class, min_vram_mb=1000)
    assert job["status"] == "queued"
    envs = await _envs()
    assert envs["pytorch-stable-cu124"].image_digest == old
    assert envs["pytorch-stable-cu124"].cuda_preflight_ok is True

    async with db() as session:
        await ensure_mirror_builds(session)
        await session.commit()
    async with db() as session:
        build = await session.scalar(
            select(EnvironmentBuild).where(EnvironmentBuild.environment_id == envs["pytorch-stable-cu124"].id)
        )
        assert build.state == "pending"
        assert build.base_image == PYTORCH_IMAGE  # re-pulled from upstream, not the dead registry
        assert build.spec == EXPECTED_TAGS["pytorch-stable-cu124"]
    assert portal.get_job(job["id"])["status"] == "queued"

    # Cancel so the queued job does not wait for a node forever.
    portal.cancel(job["id"])


async def test_pip_builds_go_before_mirrors(client, mirroring):
    async with db() as session:
        await ensure_mirror_builds(session)
        await session.commit()
    gpu_class = f"ord-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    portal.submit_job(gpu_class=gpu_class, min_vram_mb=1000, requirements=f"pkg-{uuid.uuid4().hex[:8]}==1.0\n")
    builder = _builder(client)
    first = _claim_build(client, builder)
    assert first is not None and first["kind"] == "pip"
    second = _claim_build(client, builder)
    assert second is not None and second["kind"] == "mirror"


async def test_no_mirrors_without_registry_or_when_disabled(client, mirroring, monkeypatch):
    monkeypatch.setattr(settings, "registry_url", "")
    async with db() as session:
        assert await ensure_mirror_builds(session) == 0
    monkeypatch.undo()
    monkeypatch.setattr(settings, "mirror_catalog_images", False)
    async with db() as session:
        assert await ensure_mirror_builds(session) == 0


async def test_a_retired_catalog_image_is_not_mirrored(client, mirroring):
    from datetime import datetime, timezone

    from app.envbuild import mirror_hash

    async with db() as session:
        retired = Environment(
            id=uuid.uuid4(), name=f"retired-{uuid.uuid4().hex[:8]}", framework="python", python_version="3.12", cuda_version="none",
            gpu_archs=["cpu"], image_digest="python:3.12.7-slim", retired_at=datetime.now(timezone.utc),
        )
        retired_hash = mirror_hash(retired)
        session.add(retired)
        await session.commit()
        assert await ensure_mirror_builds(session) == 3  # the three live catalog images only
        await session.commit()
        built = await session.scalar(select(EnvironmentBuild).where(EnvironmentBuild.environment_hash == retired_hash))
        assert built is None


async def test_seed_has_three_catalog_rows(client):
    envs = await _envs()
    assert {"pytorch-stable-cu124", "python-cpu", "tensorflow-stable"} <= set(envs)
    cpu = envs["python-cpu"]
    assert cpu.id == PYTHON_CPU_ID
    assert cpu.image_digest == PYTHON_CPU_IMAGE
    assert cpu.gpu_archs == ["cpu"]
    tf = envs["tensorflow-stable"]
    assert tf.image_digest == TENSORFLOW_IMAGE == "tensorflow/tensorflow:2.17.0-gpu"
    assert tf.gpu_archs == ["8.9", "8.6", "7.5", "cpu"]
    listed = {e["name"] for e in client.get("/environments", headers={"X-Dev-User": "a@uni.edu"}).json()["environments"]}
    assert {"python-cpu", "tensorflow-stable", "pytorch-stable-cu124"} <= listed


async def test_seed_upgrades_only_the_tensorflow_placeholder(client):
    async def tf_image() -> str:
        return (await _envs())["tensorflow-stable"].image_digest

    async def set_tf(**values) -> None:
        async with db() as session:
            await session.execute(update(Environment).where(Environment.id == TENSORFLOW_ID).values(**values))
            await session.commit()

    async def run_seed() -> None:
        async with db() as session:
            await seed(session)

    await set_tf(image_digest="python:3.12.7-slim")  # the old placeholder
    await run_seed()
    assert await tf_image() == TENSORFLOW_IMAGE

    await set_tf(image_digest="registry.example/custom/tf:1")  # pinned by an admin
    await run_seed()
    assert await tf_image() == "registry.example/custom/tf:1"

    await set_tf(image_digest=TENSORFLOW_IMAGE)
