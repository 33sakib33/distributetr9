"""Admin jobs API (B14): every user's jobs, admin cancel, requeue, bulk cancel."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.config import settings
from harness.client import FakeAgent
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _caps(tag: str) -> dict:
    return {"compatibility_class": tag, "gpu_model": tag, "vram_total_mb": 0, "vram_allocatable_mb": 0}


def _agent(client, tag: str, hostname: str | None = None) -> FakeAgent:
    agent = FakeAgent(client, hostname=hostname or f"adm-{tag}", caps=_caps(tag))
    agent.register()
    agent.heartbeat()
    return agent


def _list(client, **params) -> dict:
    resp = client.get("/admin/jobs", params=params, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ids(payload: dict) -> list[str]:
    return [item["id"] for item in payload["items"]]


def _finish(agent: FakeAgent, attempt: dict, *, exit_code: int = 1, category: str | None = "user_code") -> dict:
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    agent.event(attempt, "uploading")
    return agent.complete(attempt, exit_code=exit_code, failure_category=category)


def test_the_list_shows_every_users_jobs_with_owner_and_project(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    alice, bob = Portal(client), Portal(client)
    first = alice.submit_job(gpu_class=tag)
    second = bob.submit_job(gpu_class=tag)
    alice.submit_job(gpu_class=f"other-{tag}")

    payload = _list(client, gpu_class=tag)
    assert set(_ids(payload)) == {first["id"], second["id"]}
    assert payload["next_cursor"] is None
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[first["id"]]["user_email"] == alice.email
    assert by_id[second["id"]]["user_email"] == bob.email
    assert by_id[first["id"]]["project_name"]
    assert by_id[first["id"]]["status"] == "queued"
    assert "latest_attempt" in by_id[first["id"]]


def test_filters_narrow_the_list(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    alice, bob = Portal(client), Portal(client)
    queued = alice.submit_job(gpu_class=tag)
    cancelled = bob.submit_job(gpu_class=tag)
    portal_cancel = bob.cancel(cancelled["id"])
    assert portal_cancel["status_code"] == 200

    assert _ids(_list(client, gpu_class=tag, status="queued")) == [queued["id"]]
    assert _ids(_list(client, gpu_class=tag, status="cancelled")) == [cancelled["id"]]
    assert set(_ids(_list(client, gpu_class=tag, status="queued,cancelled"))) == {queued["id"], cancelled["id"]}
    # the e-mail filter is a case-insensitive substring
    assert _ids(_list(client, gpu_class=tag, user=alice.email.split("@")[0].upper())) == [queued["id"]]
    assert _ids(_list(client, gpu_class=tag, project_id=cancelled["project_id"])) == [cancelled["id"]]
    assert _list(client, gpu_class=tag, user="nobody-has-this-name")["items"] == []

    created = datetime.fromisoformat(queued["created_at"])
    assert queued["id"] in _ids(_list(client, gpu_class=tag, since=created.isoformat()))
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert _list(client, gpu_class=tag, since=future)["items"] == []
    assert _list(client, gpu_class=tag, until=(created - timedelta(days=1)).isoformat())["items"] == []


def test_node_filter_matches_the_hostname_of_a_job_s_attempt(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    host = f"Bench-{tag}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    portal.submit_job(gpu_class=tag)
    agent = _agent(client, tag, hostname=host)
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == job["id"]

    found = _list(client, node=host.lower())
    assert _ids(found) == [job["id"]]
    assert found["items"][0]["latest_attempt"]["hostname"] == host
    assert _list(client, node=f"no-such-pc-{tag}")["items"] == []


def test_paging_walks_the_whole_list_once(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    made = [portal.submit_job(gpu_class=tag)["id"] for _ in range(5)]
    portal.cancel(made[1])

    def walk(**params) -> list[str]:
        seen, cursor = [], None
        for _ in range(10):
            page = _list(client, gpu_class=tag, limit=2, **({"cursor": cursor} if cursor else {}), **params)
            assert len(page["items"]) <= 2
            seen += _ids(page)
            cursor = page["next_cursor"]
            if cursor is None:
                return seen
        raise AssertionError("paging did not end")

    assert walk() == list(reversed(made))  # newest first, nothing twice
    assert walk(status="queued") == [m for m in reversed(made) if m != made[1]]


def test_bad_parameters_are_rejected(client):
    assert client.get("/admin/jobs", params={"status": "bogus"}, headers=ADMIN).status_code == 400
    assert client.get("/admin/jobs", params={"cursor": "not-a-cursor"}, headers=ADMIN).status_code == 400
    assert client.get("/admin/jobs", params={"since": "yesterday-ish"}, headers=ADMIN).status_code == 400
    assert client.get("/admin/jobs", params={"limit": 0}, headers=ADMIN).status_code == 422
    assert client.get("/admin/jobs", params={"limit": 201}, headers=ADMIN).status_code == 422


def test_only_admins_reach_the_admin_job_endpoints(client, monkeypatch):
    Portal(client).ensure_project()  # makes sure the dev admin exists
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get("/admin/jobs", headers=student).status_code == 403
    assert client.post(f"/admin/jobs/{uuid.uuid4()}/requeue", headers=student).status_code == 403
    assert client.post("/admin/jobs/cancel", json={"job_ids": [str(uuid.uuid4())]}, headers=student).status_code == 403


def test_an_admin_cancels_a_job_in_a_project_they_do_not_belong_to(client, monkeypatch):
    Portal(client).ensure_project()
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    owner = Portal(client)
    other = Portal(client)
    job = owner.submit_job(gpu_class=f"adm-{uuid.uuid4().hex[:8]}")

    assert other.cancel(job["id"])["status_code"] == 403
    resp = client.post(f"/jobs/{job['id']}/cancel", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert owner.get_job(job["id"])["status"] == "cancelled"


def test_requeue_makes_a_failed_job_runnable_again(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    job = portal.submit_job(gpu_class=tag)
    agent = _agent(client, tag)
    first = agent.claim()
    assert _finish(agent, first)["status_code"] == 200
    failed = portal.get_job(job["id"])
    assert failed["status"] == "failed"

    resp = client.post(f"/admin/jobs/{job['id']}/requeue", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["pending_reason"] == "requeued by admin"
    assert body["max_attempts"] == 1 + 3  # attempts used so far, plus three
    assert body["accepted_attempt_id"] is None

    second = agent.claim()
    assert second is not None and second["job_id"] == job["id"]
    assert agent.run_to_complete(second)["status_code"] == 200
    done = portal.get_job(job["id"])
    assert done["status"] == "completed"
    assert done["latest_attempt"]["attempt_number"] == 2


def test_requeue_brings_back_a_cancelled_job_with_or_without_an_attempt(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    never_ran = portal.submit_job(gpu_class=tag)
    portal.cancel(never_ran["id"])
    assert portal.get_job(never_ran["id"])["status"] == "cancelled"
    resp = client.post(f"/admin/jobs/{never_ran['id']}/requeue", headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "queued"
    assert resp.json()["max_attempts"] == 3  # a job with no attempts keeps the default

    agent = _agent(client, tag)
    attempt = agent.claim()
    assert attempt is not None and attempt["job_id"] == never_ran["id"]
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    portal.cancel(never_ran["id"])
    cancelled = agent.complete(attempt, exit_code=143, failure_category="cancelled")
    assert cancelled["body"]["status"] == "cancelled", cancelled  # the agent's answer to a cancel is a 409 "not accepted"
    assert portal.get_job(never_ran["id"])["status"] == "cancelled"

    again = client.post(f"/admin/jobs/{never_ran['id']}/requeue", headers=ADMIN)
    assert again.status_code == 200, again.text
    assert again.json()["status"] == "queued"  # the old cancelled attempt does not keep it cancelled
    assert again.json()["max_attempts"] == 1 + 3
    retried = agent.claim()
    assert retried is not None and retried["job_id"] == never_ran["id"]


def test_requeue_refuses_jobs_that_are_not_finished_or_do_not_exist(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    queued = portal.submit_job(gpu_class=tag)
    resp = client.post(f"/admin/jobs/{queued['id']}/requeue", headers=ADMIN)
    assert resp.status_code == 409, resp.text

    agent = _agent(client, tag)
    attempt = agent.claim()
    agent.run_to_complete(attempt)
    assert portal.get_job(queued["id"])["status"] == "completed"
    assert client.post(f"/admin/jobs/{queued['id']}/requeue", headers=ADMIN).status_code == 409
    assert client.post(f"/admin/jobs/{uuid.uuid4()}/requeue", headers=ADMIN).status_code == 404


def test_bulk_cancel_reports_what_it_did(client):
    tag = f"adm-{uuid.uuid4().hex[:8]}"
    portal = Portal(client)
    running = portal.submit_job(gpu_class=tag)
    agent = _agent(client, tag)
    attempt = agent.claim()
    agent.acknowledge(attempt)
    agent.event(attempt, "running")
    waiting = portal.submit_job(gpu_class=tag)
    done = portal.submit_job(gpu_class=f"other-{tag}")
    other = _agent(client, f"other-{tag}")
    other.run_to_complete(other.claim())
    ghost = str(uuid.uuid4())

    resp = client.post(
        "/admin/jobs/cancel",
        json={"job_ids": [running["id"], waiting["id"], waiting["id"], done["id"], ghost]},
        headers=ADMIN,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cancelled"] == [running["id"], waiting["id"]]
    skipped = {item["job_id"]: item["reason"] for item in body["skipped"]}
    assert skipped[done["id"]] == "already completed"
    assert skipped[ghost] == "job not found"
    assert portal.get_job(waiting["id"])["status"] == "cancelled"
    assert portal.get_job(running["id"])["status"] == "cancelling"  # the agent still has to stop it


def test_bulk_cancel_takes_between_one_and_two_hundred_ids(client):
    assert client.post("/admin/jobs/cancel", json={"job_ids": []}, headers=ADMIN).status_code == 422
    ids = [str(uuid.uuid4()) for _ in range(201)]
    assert client.post("/admin/jobs/cancel", json={"job_ids": ids}, headers=ADMIN).status_code == 422
    resp = client.post("/admin/jobs/cancel", json={"job_ids": ids[:200]}, headers=ADMIN)
    assert resp.status_code == 200 and len(resp.json()["skipped"]) == 200
