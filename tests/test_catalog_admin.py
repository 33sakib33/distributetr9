"""Catalog management (B19): admins add, edit, retire and restore catalog images."""
from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import update

from app.config import settings
from app.models import Environment
from harness.client import FakeAgent
from harness.jobs import Portal, make_archive
from tests.dbhelp import with_session

ADMIN = {"X-Dev-User": "admin@uni.edu"}


@pytest.fixture
def catalog(client):
    """Create catalog rows through the API; retire them afterwards so the shared
    database keeps exactly the three seeded images in use."""
    made: list[str] = []

    def add(**changes) -> dict:
        body = {
            "name": f"adm-{uuid.uuid4().hex[:8]}",
            "framework": "python",
            "python_version": "3.12",
            "cuda_version": "none",
            "gpu_archs": ["cpu"],
            "image_digest": "python:3.12.7-slim",
            **changes,
        }
        resp = client.post("/admin/environments", json=body, headers=ADMIN)
        assert resp.status_code == 201, resp.text
        made.append(resp.json()["id"])
        return resp.json()

    yield add
    for env_id in made:
        client.patch(f"/admin/environments/{env_id}", json={"retired": True}, headers=ADMIN)


def _submit(client, portal: Portal, project: str, env_id: str, tag: str, expect: int = 201) -> dict:
    data = {
        "project_id": project, "entrypoint": "train.py", "environment_id": env_id, "args": "[]",
        "resources": json.dumps({"gpu_class": tag, "max_runtime_seconds": 120}),
    }
    files = {"archive": ("a.tar.gz", make_archive(), "application/gzip")}
    resp = client.post("/jobs", data=data, files=files, headers=portal.headers)
    assert resp.status_code == expect, resp.text
    return resp.json()


def _visible(client, who: str = "alice@uni.edu") -> dict[str, dict]:
    resp = client.get("/environments", headers={"X-Dev-User": who})
    assert resp.status_code == 200, resp.text
    return {e["name"]: e for e in resp.json()["environments"]}


def _admin_list(client) -> dict[str, dict]:
    resp = client.get("/admin/environments", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["next_cursor"] is None
    return {e["name"]: e for e in resp.json()["items"]}


def test_a_new_catalog_image_is_listed_and_usable(client, catalog):
    env = catalog(gpu_archs=["8.6", "cpu"], image_digest="  python:3.12.9-slim ")
    assert env["image_digest"] == "python:3.12.9-slim"
    assert env["gpu_archs"] == ["8.6", "cpu"] and env["retired"] is False and env["jobs_last_30d"] == 0
    assert env["cuda_preflight_ok"] is True
    assert env["name"] in _visible(client)
    portal = Portal(client)
    job = _submit(client, portal, portal.ensure_project(), env["id"], uuid.uuid4().hex[:8])
    assert job["environment_id"] == env["id"]
    assert _admin_list(client)[env["name"]]["jobs_last_30d"] == 1


def test_names_must_be_unique_and_not_reserved(client, catalog):
    env = catalog()
    body = {k: env[k] for k in ("framework", "python_version", "cuda_version", "gpu_archs", "image_digest")}
    clash = client.post("/admin/environments", json={**body, "name": env["name"].upper()}, headers=ADMIN)
    assert clash.status_code == 409
    reserved = client.post("/admin/environments", json={**body, "name": "build-abc123"}, headers=ADMIN)
    assert reserved.status_code == 400
    assert client.post("/admin/environments", json={**body, "name": "has space"}, headers=ADMIN).status_code == 422
    assert client.post("/admin/environments", json={**body, "name": "x", "gpu_archs": []}, headers=ADMIN).status_code == 422
    assert client.post("/admin/environments", json={**body, "name": "x", "image_digest": " "}, headers=ADMIN).status_code == 422
    other = catalog()
    rename = client.patch(f"/admin/environments/{other['id']}", json={"name": env["name"]}, headers=ADMIN)
    assert rename.status_code == 409
    assert client.patch(f"/admin/environments/{other['id']}", json={"name": "build-x"}, headers=ADMIN).status_code == 400


def test_edit_changes_only_what_is_sent(client, catalog):
    env = catalog()
    resp = client.patch(
        f"/admin/environments/{env['id']}",
        json={"framework": "jax", "python_version": "3.11", "gpu_archs": ["8.9", "8.9", "cpu"]},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    got = resp.json()
    assert (got["framework"], got["python_version"], got["gpu_archs"]) == ("jax", "3.11", ["8.9", "cpu"])
    assert got["name"] == env["name"] and got["image_digest"] == env["image_digest"]
    assert client.patch(f"/admin/environments/{uuid.uuid4()}", json={"framework": "x"}, headers=ADMIN).status_code == 404
    assert client.patch(f"/admin/environments/{env['id']}", json={"cuda_version": ""}, headers=ADMIN).status_code == 422


def test_a_retired_image_is_hidden_and_refused_but_running_jobs_finish(client, catalog):
    env = catalog()
    tag = uuid.uuid4().hex[:8]
    portal = Portal(client)
    project = portal.ensure_project()

    queued = _submit(client, portal, project, env["id"], tag)
    retired = client.patch(f"/admin/environments/{env['id']}", json={"retired": True}, headers=ADMIN)
    assert retired.status_code == 200 and retired.json()["retired"] is True and retired.json()["retired_at"]
    assert env["name"] not in _visible(client)
    assert _admin_list(client)[env["name"]]["retired"] is True

    refused = _submit(client, portal, project, env["id"], tag, 400)
    assert "retired" in refused["message"]

    # the job submitted before still runs
    node = FakeAgent(client, hostname=f"cat-{tag}", caps={
        "compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0})
    node.register()
    node.heartbeat()
    attempt = node.claim()
    assert attempt is not None and attempt["job_id"] == queued["id"]
    node.run_to_complete(attempt)
    assert portal.get_job(queued["id"])["status"] == "completed"

    restored = client.patch(f"/admin/environments/{env['id']}", json={"retired": False}, headers=ADMIN)
    assert restored.json()["retired"] is False and restored.json()["retired_at"] is None
    assert env["name"] in _visible(client)
    _submit(client, portal, project, env["id"], tag)


def test_admin_list_counts_jobs_of_the_last_30_days(client, catalog):
    env = catalog()
    assert _admin_list(client)[env["name"]]["jobs_last_30d"] == 0
    names = _admin_list(client)
    assert {"pytorch-stable-cu124", "tensorflow-stable", "python-cpu"} <= set(names)
    assert not any(name.startswith("build-") for name in names)
    cpu = names["python-cpu"]
    Portal(client).submit_job()  # uses pytorch-stable-cu124 by default
    after = _admin_list(client)
    assert after["pytorch-stable-cu124"]["jobs_last_30d"] >= 1
    assert after["python-cpu"]["jobs_last_30d"] == cpu["jobs_last_30d"]


async def test_a_new_public_image_forgets_the_old_mirror_source_but_a_registry_digest_keeps_it(client, catalog):
    env = catalog(image_digest="python:3.12.7-slim")

    async def set_upstream(session):
        await session.execute(
            update(Environment).where(Environment.id == uuid.UUID(env["id"])).values(upstream_image="python:3.12.7-slim")
        )

    await with_session(set_upstream)
    on_registry = f"{settings.registry_url}/lab/{env['name']}@sha256:" + "ab" * 32
    kept = client.patch(f"/admin/environments/{env['id']}", json={"image_digest": on_registry}, headers=ADMIN)
    assert kept.json()["upstream_image"] == "python:3.12.7-slim"

    resp = client.patch(f"/admin/environments/{env['id']}", json={"image_digest": "python:3.12.9-slim"}, headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["image_digest"] == "python:3.12.9-slim" and resp.json()["upstream_image"] is None


def test_catalog_admin_is_for_admins(client, monkeypatch):
    client.get("/me", headers=ADMIN)  # the admin must exist before new users stop being admins
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/environments", headers=student).status_code == 403
    body = {"name": "x", "framework": "f", "python_version": "3", "cuda_version": "n", "gpu_archs": ["cpu"], "image_digest": "i"}
    assert client.post("/admin/environments", json=body, headers=student).status_code == 403
    assert client.patch(f"/admin/environments/{uuid.uuid4()}", json={"retired": True}, headers=student).status_code == 403
    assert client.get("/environments").status_code == 401  # and the student list needs a sign-in
