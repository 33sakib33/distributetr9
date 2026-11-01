"""Availability and wait estimates (B23), queue position (B24), clone (B27),
outputs as one zip (B28) and the extra fields of /nodes and /admin/projects (X3, X4)."""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from app import availability
from app.config import settings
from app.models import Job, Node
from dbhelp import with_session
from harness.client import FakeAgent
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _caps(tag: str, vram: int = 0) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": vram, "vram_allocatable_mb": vram}


def _tag() -> str:
    return f"sv-{uuid.uuid4().hex[:8]}"


def _agent(client, tag: str, *, vram: int = 0, name: str | None = None, health: dict | None = None) -> FakeAgent:
    agent = FakeAgent(client, hostname=name or f"sv-{tag}", caps=_caps(tag, vram))
    agent.register()
    agent.heartbeat(health=health)
    return agent


def _row(client, tag: str, headers: dict | None = None) -> dict | None:
    resp = client.get("/availability", headers=headers or ADMIN)
    assert resp.status_code == 200, resp.text
    return next((c for c in resp.json()["classes"] if c["gpu_class"] == tag), None)


@pytest.fixture
def median(monkeypatch):
    async def fixed(_session, _now):
        return 100

    monkeypatch.setattr(availability, "median_runtime_seconds", fixed)


# --- Estimates (pure) --------------------------------------------------------------


def test_the_wait_formula():
    wait = availability.estimate_wait_seconds
    assert wait(queued=5, online=2, free=1, median=100) == 0
    assert wait(queued=0, online=0, free=0, median=100) is None
    assert wait(queued=0, online=1, free=0, median=100) == 100
    assert wait(queued=4, online=2, free=0, median=100) == 300  # ceil(5 / 2) = 3 rounds
    assert wait(queued=10_000, online=1, free=0, median=3600) == 24 * 3600  # capped


def test_the_start_estimate_lets_jobs_ahead_take_free_pcs_first():
    start = availability.estimate_start_seconds
    assert start(position=1, online=2, free=2, median=100) == 0
    assert start(position=2, online=2, free=2, median=100) == 0
    assert start(position=3, online=2, free=2, median=100) == 100
    assert start(position=1, online=0, free=0, median=100) is None
    assert start(position=3, online=1, free=0, median=100) == 300


# --- /availability -----------------------------------------------------------------


def test_a_class_with_a_free_pc_has_no_wait(client, median):
    tag = _tag()
    assert _row(client, tag) is None
    _agent(client, tag, vram=12000)
    Portal(client).submit_job(gpu_class=tag)
    row = _row(client, tag, Portal(client).headers)  # any signed-in user may ask
    assert row == {
        "gpu_class": tag,
        "gpu_model": tag,
        "vram_mb": 12000,
        "nodes_total": 1,
        "nodes_online": 1,
        "nodes_free": 1,
        "queued": 1,
        "est_wait_seconds": 0,
    }


def test_a_busy_class_waits_for_the_queue_in_medians(client, median):
    tag = _tag()
    agent = _agent(client, tag)
    portal = Portal(client)
    portal.submit_job(gpu_class=tag)
    assert agent.claim() is not None
    row = _row(client, tag)
    assert (row["nodes_online"], row["nodes_free"], row["queued"], row["est_wait_seconds"]) == (1, 0, 0, 100)
    portal.submit_job(gpu_class=tag)
    portal.submit_job(gpu_class=tag)
    row = _row(client, tag)
    assert (row["queued"], row["est_wait_seconds"]) == (2, 300)


def test_offline_draining_and_forgotten_pcs_do_not_count_as_online(client, median):
    tag = _tag()
    live, stale, drained = (_agent(client, tag, name=f"sv-{n}-{tag}") for n in ("a", "b", "c"))
    client.post(f"/nodes/{drained.node_id}/drain", headers=ADMIN)

    async def age(session):
        await session.execute(
            update(Node).where(Node.id == uuid.UUID(stale.node_id)).values(
                last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
        )

    import asyncio

    asyncio.run(with_session(age))
    row = _row(client, tag)
    assert (row["nodes_total"], row["nodes_online"], row["nodes_free"]) == (3, 1, 1)

    client.post(f"/nodes/{live.node_id}/drain", headers=ADMIN)
    row = _row(client, tag)
    assert row["nodes_online"] == 0 and row["est_wait_seconds"] is None

    for node in (live, stale, drained):
        assert client.post(f"/nodes/{node.node_id}/forget", headers=ADMIN).status_code == 200
    assert _row(client, tag) is None


def test_builders_are_not_a_class_and_availability_needs_a_sign_in(client):
    tag = _tag()
    builder = FakeAgent(client, hostname=f"sv-build-{tag}", caps=_caps(tag), role="build")
    builder.register()
    builder.heartbeat()
    assert _row(client, tag) is None
    assert client.get("/availability").status_code == 401


def _age(agent: FakeAgent, days: float) -> None:
    import asyncio

    async def go(session):
        await session.execute(
            update(Node).where(Node.id == uuid.UUID(agent.node_id)).values(
                last_heartbeat_at=datetime.now(timezone.utc) - timedelta(days=days)
            )
        )

    asyncio.run(with_session(go))


def test_a_class_nobody_has_heard_from_in_a_week_is_not_listed(client, median):
    tag = _tag()
    agent = _agent(client, tag)
    assert _row(client, tag) is not None
    _age(agent, 6)
    assert _row(client, tag) is not None
    _age(agent, 8)
    assert _row(client, tag) is None
    assert _row(client, "cpu") is not None  # the cpu card stays


def test_the_window_is_a_setting(client, median, monkeypatch):
    tag = _tag()
    _age(_agent(client, tag), 3)
    assert _row(client, tag) is not None
    monkeypatch.setattr(settings, "availability_class_max_age_days", 2)
    assert _row(client, tag) is None


def test_the_cpu_row_counts_every_train_node_and_never_a_builder(client, median):
    before = _row(client, "cpu")
    assert before is not None and before["gpu_model"] is None and before["vram_mb"] == 0
    tag = _tag()
    _agent(client, tag, vram=8000, name=f"sv-a-{tag}")
    _agent(client, f"{tag}-b", name=f"sv-b-{tag}")
    FakeAgent(client, hostname=f"sv-build-{tag}", caps=_caps(tag), role="build").register()
    after = _row(client, "cpu")
    assert after["nodes_total"] == before["nodes_total"] + 2
    assert after["nodes_online"] == before["nodes_online"] + 2
    assert after["nodes_free"] == before["nodes_free"] + 2


def test_a_class_names_its_most_common_gpu_model(client, median):
    tag = _tag()
    caps = lambda model: {**_caps(tag, 8000), "gpu_model": model}  # noqa: E731
    for i, model in enumerate(("RTX 3060", "RTX 3060", "RTX 3070")):
        pc = FakeAgent(client, hostname=f"sv-{i}-{tag}", caps=caps(model))
        pc.register()
        pc.heartbeat()
    row = _row(client, tag)
    assert row["nodes_total"] == 3 and row["gpu_model"] == "RTX 3060"


def test_a_job_shows_its_owner_and_project(client):
    portal = Portal(client)
    job = portal.submit_job()
    detail = portal.get_job(job["id"])
    assert detail["user_email"] == portal.email
    projects = client.get("/projects", headers=portal.headers).json()["projects"]
    assert detail["project_name"] == next(p["name"] for p in projects if p["id"] == detail["project_id"])


# --- Queue position ----------------------------------------------------------------


def test_queue_position_follows_priority_then_age(client, median):
    tag = _tag()
    portal = Portal(client)
    first, second, third = (portal.submit_job(gpu_class=tag) for _ in range(3))
    assert [portal.get_job(j["id"])["queue_position"] for j in (first, second, third)] == [1, 2, 3]
    assert portal.get_job(third["id"])["est_start_seconds"] is None  # no PC of the class yet

    async def bump(session):
        await session.execute(update(Job).where(Job.id == uuid.UUID(third["id"])).values(priority=5))

    import asyncio

    asyncio.run(with_session(bump))
    assert [portal.get_job(j["id"])["queue_position"] for j in (third, first, second)] == [1, 2, 3]


def test_a_queued_job_gets_an_estimated_start(client, median):
    tag = _tag()
    agent = _agent(client, tag)
    portal = Portal(client)
    running, next_up, after = (portal.submit_job(gpu_class=tag) for _ in range(3))
    # a free PC: the head of the line starts at once
    assert portal.get_job(running["id"])["est_start_seconds"] == 0
    assert agent.claim()["job_id"] == running["id"]
    assert portal.get_job(next_up["id"])["queue_position"] == 1
    assert portal.get_job(next_up["id"])["est_start_seconds"] == 100
    assert portal.get_job(after["id"])["est_start_seconds"] == 200


def test_jobs_that_are_not_queued_have_no_position(client):
    tag = _tag()
    agent = _agent(client, tag)
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    attempt = agent.claim()
    got = portal.get_job(job["id"])
    assert got["status"] == "starting"
    assert got["queue_position"] is None and got["est_start_seconds"] is None
    agent.run_to_complete(attempt)
    done = portal.get_job(job["id"])
    assert done["status"] == "completed" and done["queue_position"] is None


# --- /nodes (X3) -------------------------------------------------------------------


def test_nodes_show_load_temperature_and_the_job_they_run(client):
    tag = _tag()
    busy = _agent(client, tag, name=f"sv-busy-{tag}", health={"gpu_temp_c": 61.5, "gpu_pct": 88.0})
    idle = _agent(client, tag, name=f"sv-idle-{tag}", health={"gpu_temp_c": 35.0, "gpu_pct": 0.0})
    silent = _agent(client, tag, name=f"sv-silent-{tag}")
    job = Portal(client).submit_job(gpu_class=tag)
    attempt = busy.claim()
    assert attempt is not None

    nodes = {n["hostname"]: n for n in client.get("/nodes", headers=ADMIN).json()["nodes"]}
    quiet = nodes[silent.hostname]
    assert (quiet["gpu_pct"], quiet["gpu_temp_c"], quiet["busy"], quiet["current_attempt"]) == (None, None, False, None)
    assert (nodes[idle.hostname]["gpu_temp_c"], nodes[idle.hostname]["gpu_pct"]) == (35.0, 0.0)
    assert nodes[idle.hostname]["busy"] is False and nodes[idle.hostname]["current_attempt"] is None
    shown = nodes[busy.hostname]
    assert (shown["gpu_temp_c"], shown["gpu_pct"], shown["busy"]) == (61.5, 88.0, True)
    assert shown["current_attempt"] == {"attempt_id": attempt["attempt_id"], "job_id": job["id"], "state": "leased"}

    busy.acknowledge(attempt)
    busy.event(attempt, "running")
    nodes = {n["hostname"]: n for n in client.get("/nodes", headers=ADMIN).json()["nodes"]}
    assert nodes[busy.hostname]["current_attempt"]["state"] == "running"
    busy.event(attempt, "uploading")
    busy.complete(attempt)
    nodes = {n["hostname"]: n for n in client.get("/nodes", headers=ADMIN).json()["nodes"]}
    assert nodes[busy.hostname]["busy"] is False and nodes[busy.hostname]["current_attempt"] is None


def test_an_old_health_sample_is_not_shown_as_current_load(client):
    tag = _tag()
    agent = _agent(client, tag, health={"gpu_temp_c": 70.0, "gpu_pct": 50.0})

    async def age(session):
        from app.models import NodeHealth

        await session.execute(
            update(NodeHealth).where(NodeHealth.node_id == uuid.UUID(agent.node_id)).values(
                at=datetime.now(timezone.utc) - timedelta(hours=1)
            )
        )

    import asyncio

    asyncio.run(with_session(age))
    node = next(n for n in client.get("/nodes", headers=ADMIN).json()["nodes"] if n["hostname"] == agent.hostname)
    assert node["gpu_pct"] is None and node["gpu_temp_c"] is None


def test_listing_nodes_needs_the_same_number_of_queries_for_any_fleet(client):
    from sqlalchemy import event

    from app.db import engine

    counts = []
    for extra in (0, 4):
        tag = _tag()
        for i in range(extra):
            _agent(client, tag, name=f"sv-q{i}-{tag}").heartbeat(health={"gpu_pct": 1.0})
        seen = []

        def count(_conn, _cursor, statement, *_a):
            seen.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", count)
        try:
            assert client.get("/nodes", headers=ADMIN).status_code == 200
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", count)
        counts.append(len(seen))
    assert counts[0] == counts[1], counts


# --- /admin/projects (X4) ----------------------------------------------------------


def test_admin_projects_lists_name_owner_members_and_quota(client):
    owner, friend = Portal(client), Portal(client)
    tag = uuid.uuid4().hex[:8]
    big = client.post("/projects", json={"name": f"Zeta-{tag}"}, headers=owner.headers).json()["id"]
    client.post(f"/projects/{big}/members", json={"email": friend.email}, headers=owner.headers)
    client.post("/projects", json={"name": f"alpha-{tag}"}, headers=owner.headers)
    client.put(f"/admin/quotas/project/{big}", json={"max_concurrent_jobs": 7}, headers=ADMIN)

    resp = client.get("/admin/projects", params={"q": tag.upper()}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [i["name"] for i in items] == [f"alpha-{tag}", f"Zeta-{tag}"]  # by name, any case
    zeta = items[1]
    assert zeta == {
        "id": big,
        "name": f"Zeta-{tag}",
        "created_by_email": owner.email,
        "members": 2,
        "max_concurrent_jobs": 7,
    }
    assert items[0]["members"] == 1 and items[0]["max_concurrent_jobs"] > 0


def test_admin_projects_pages_and_is_for_admins(client, monkeypatch):
    owner = Portal(client)
    tag = uuid.uuid4().hex[:8]
    for i in range(5):
        client.post("/projects", json={"name": f"pg-{tag}-{i}"}, headers=owner.headers)
    seen, cursor = [], None
    for _ in range(6):
        page = client.get(
            "/admin/projects", params={"q": f"pg-{tag}", "limit": 2, **({"cursor": cursor} if cursor else {})}, headers=ADMIN
        ).json()
        seen += [i["name"] for i in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"pg-{tag}-{i}" for i in range(5)]
    assert client.get("/admin/projects", params={"limit": 0}, headers=ADMIN).status_code == 422

    monkeypatch.setattr(settings, "dev_auth_admin", False)
    assert client.get("/admin/projects", headers={"X-Dev-User": f"s-{tag}@uni.edu"}).status_code == 403


# --- Clone (B27) -------------------------------------------------------------------


def _clone(client, portal: Portal, job_id: str, body: dict | None = None):
    return client.post(f"/jobs/{job_id}/clone", json=body, headers=portal.headers)


def test_a_clone_copies_the_upload_and_settings(client):
    portal = Portal(client)
    tag = _tag()
    source = portal.submit_job(gpu_class=tag, min_vram_mb=0, max_runtime_seconds=90)
    resp = _clone(client, portal, source["id"])
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["id"] != source["id"]
    assert (copy["project_id"], copy["entrypoint"], copy["args"], copy["environment_id"]) == (
        source["project_id"], source["entrypoint"], source["args"], source["environment_id"],
    )
    assert copy["resources"] == source["resources"]
    assert copy["status"] == "queued"
    from app.storage import storage

    original = storage.get_bytes(f"submissions/{source['id']}/input.tar.gz")
    assert storage.get_bytes(f"submissions/{copy['id']}/input.tar.gz") == original
    # it runs: a PC of the class gets the copy's own upload
    agent = _agent(client, tag)
    claimed = agent.claim()
    assert claimed["job_id"] in {source["id"], copy["id"]}


def test_a_clone_can_change_arguments_resources_and_entrypoint(client):
    from tests.test_upload import _zip

    portal = Portal(client)
    tag = _tag()
    data = {
        "project_id": portal.ensure_project(),
        "environment_id": portal.environment_id(),
        "entrypoint": "a.py",
        "args": json.dumps(["--lr", "1"]),
        "resources": json.dumps({"gpu_class": tag, "ram_mb": 1024, "max_runtime_seconds": 60}),
    }
    files = {"archive": ("p.zip", _zip({"a.py": "", "b.py": ""}), "application/zip")}
    source = client.post("/jobs", data=data, files=files, headers=portal.headers).json()

    resp = _clone(client, portal, source["id"], {"args": ["--lr", "2"], "entrypoint": "b.py",
                                                 "resources": {"ram_mb": 4096, "cpu_cores": 2}})
    assert resp.status_code == 201, resp.text
    copy = resp.json()
    assert copy["args"] == ["--lr", "2"] and copy["entrypoint"] == "b.py"
    assert copy["resources"]["ram_mb"] == 4096 and copy["resources"]["cpu_cores"] == 2
    assert copy["resources"]["gpu_class"] == tag and copy["resources"]["max_runtime_seconds"] == 60
    assert client.get(f"/jobs/{source['id']}", headers=portal.headers).json()["args"] == ["--lr", "1"]
    from app.storage import storage

    assert storage.list_keys(f"submissions/{copy['id']}/")[0].endswith("input.zip")

    assert _clone(client, portal, source["id"], {"entrypoint": "missing.py"}).status_code == 400
    assert _clone(client, portal, source["id"], {"entrypoint": "b.sh"}).status_code == 400
    assert _clone(client, portal, source["id"], {"resources": {"max_runtime_seconds": 10**9}}).status_code == 400
    assert _clone(client, portal, source["id"], {"resources": {"ram_mb": "lots"}}).status_code == 400


def test_a_clone_may_switch_to_another_catalog_environment(client):
    portal = Portal(client)
    source = portal.submit_job(gpu_class=_tag())
    envs = client.get("/environments", headers=portal.headers).json()["environments"]
    other = next(e for e in envs if e["id"] != source["environment_id"])
    resp = _clone(client, portal, source["id"], {"environment_id": other["id"]})
    assert resp.status_code == 201, resp.text
    assert resp.json()["environment_id"] == other["id"]
    assert _clone(client, portal, source["id"], {"environment_id": str(uuid.uuid4())}).status_code == 400


def test_a_clone_of_a_custom_build_reuses_the_build(client):
    portal = Portal(client)
    tag = _tag()
    req = f"pkg-{uuid.uuid4().hex[:10]}==1.0.0\n"
    source = portal.submit_job(gpu_class=tag, requirements=req)
    assert source["pending_reason"] == "building environment"
    copy = _clone(client, portal, source["id"]).json()
    assert copy["environment_id"] == source["environment_id"]
    assert copy["pending_reason"] == "building environment" and copy["status"] == "building"

    # one build serves both jobs; when it is ready both wait for a PC
    builder = FakeAgent(client, hostname=f"sv-b-{tag}", caps=_caps(tag), role="build")
    builder.register()
    builder.heartbeat()
    build = client.post("/agent/claim", json={}, headers=builder._headers()).json()["build"]
    digest = "127.0.0.1:5000/lab-env/test@sha256:" + ("cd" * 32)
    client.post(
        f"/agent/builds/{build['build_id']}/complete",
        json={"ok": True, "image_digest": digest, "lockfile": req, "log": "ok"},
        headers=builder._headers(),
    )
    assert portal.get_job(copy["id"])["status"] == "queued"
    later = _clone(client, portal, source["id"]).json()
    assert later["status"] == "queued" and later["environment_id"] == source["environment_id"]


def test_only_project_members_clone(client, monkeypatch):
    owner = Portal(client)
    source = owner.submit_job(gpu_class=_tag())
    monkeypatch.setattr(settings, "dev_auth_admin", False)  # users made from here on are students
    stranger = Portal(client)
    assert _clone(client, stranger, source["id"]).status_code == 403
    friend = Portal(client)
    client.post(f"/projects/{source['project_id']}/members", json={"email": friend.email}, headers=owner.headers)
    copy = _clone(client, friend, source["id"]).json()
    assert copy["user_id"] != source["user_id"] and copy["project_id"] == source["project_id"]
    assert _clone(client, owner, str(uuid.uuid4())).status_code == 404


# --- Outputs as one zip (B28) ------------------------------------------------------


def _finish_with_outputs(client, files: dict[str, bytes]) -> tuple[Portal, dict]:
    tag = _tag()
    agent = _agent(client, tag)
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    for name, data in files.items():
        assert agent.put_output(attempt, name, data)["body"]
    agent.event(attempt, "uploading")
    manifest = [
        {"relative_path": n, "size_bytes": len(d), "checksum_sha256": hashlib.sha256(d).hexdigest()}
        for n, d in files.items()
    ]
    resp = client.post(
        f"/attempts/{attempt['attempt_id']}/complete",
        json={"fencing_token": attempt["fencing_token"], "exit_code": 0, "output_manifest": manifest},
        headers=agent._headers(),
    )
    assert resp.status_code == 200, resp.text
    return portal, job


def test_outputs_come_as_one_zip_with_relative_paths(client):
    files = {"model.bin": b"\x00\x01" * 5000, "logs/run.txt": b"hello\n", "a/b/c.csv": b"1,2\n"}
    portal, job = _finish_with_outputs(client, files)
    resp = client.get(f"/jobs/{job['id']}/outputs.zip", headers=portal.headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert f'job-{job["id"][:8]}-outputs.zip' in resp.headers["content-disposition"]
    assert int(resp.headers["content-length"]) == len(resp.content)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == sorted(files)
        assert {n: zf.read(n) for n in zf.namelist()} == files


def test_a_job_without_outputs_has_no_zip_and_it_is_private(client, monkeypatch):
    portal, with_outputs = _finish_with_outputs(client, {"r.txt": b"x"})
    empty = portal.submit_job(gpu_class=_tag())
    assert client.get(f"/jobs/{empty['id']}/outputs.zip", headers=portal.headers).status_code == 404
    assert client.get(f"/jobs/{uuid.uuid4()}/outputs.zip", headers=portal.headers).status_code == 404

    monkeypatch.setattr(settings, "dev_auth_admin", False)
    stranger = Portal(client)
    assert client.get(f"/jobs/{with_outputs['id']}/outputs.zip", headers=stranger.headers).status_code == 403
