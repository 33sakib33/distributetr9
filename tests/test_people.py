"""People and quotas (B18): project members, the admin user list, blocking, quotas."""
from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.session_tokens import issue_session
from harness.jobs import Portal

ADMIN = {"X-Dev-User": "admin@uni.edu"}


def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@uni.edu"


def _as(email: str) -> dict:
    return {"X-Dev-User": email}


@pytest.fixture
def students(client, monkeypatch):
    """Users first seen while this fixture is active are ordinary students."""
    client.get("/me", headers=ADMIN)  # the admin must exist before new users stop being admins
    monkeypatch.setattr(settings, "dev_auth_admin", False)


def _team(client, students) -> dict:
    """A project with an owner and one member, both ordinary students."""
    owner, member = _email("owner"), _email("member")
    assert client.get("/me", headers=_as(owner)).json()["is_admin"] is False
    project = client.post("/projects", json={"name": f"team-{uuid.uuid4().hex[:6]}"}, headers=_as(owner)).json()
    added = client.post(f"/projects/{project['id']}/members", json={"email": member}, headers=_as(owner))
    assert added.status_code == 201, added.text
    return {
        "project": project["id"], "owner": owner, "member": member,
        "owner_id": client.get("/me", headers=_as(owner)).json()["id"], "member_id": added.json()["user_id"],
    }


def _members(client, team: dict, who: str | None = None) -> dict[str, dict]:
    resp = client.get(f"/projects/{team['project']}/members", headers=_as(who or team["owner"]))
    assert resp.status_code == 200, resp.text
    return {m["email"]: m for m in resp.json()["items"]}


def test_members_can_see_the_list_and_outsiders_cannot(client, students):
    team = _team(client, students)
    listed = client.get(f"/projects/{team['project']}/members", headers=_as(team["member"])).json()
    assert listed["your_role"] == "member" and listed["can_edit"] is False
    assert listed["project"]["id"] == team["project"] and listed["next_cursor"] is None
    assert [m["role"] for m in listed["items"]] == ["owner", "member"]
    assert {m["email"] for m in listed["items"]} == {team["owner"], team["member"]}

    outsider = _as(_email("outsider"))
    assert client.get(f"/projects/{team['project']}/members", headers=outsider).status_code == 403
    admin_view = client.get(f"/projects/{team['project']}/members", headers=ADMIN)
    assert admin_view.status_code == 200 and admin_view.json()["can_edit"] is True
    assert client.get(f"/projects/{uuid.uuid4()}/members", headers=ADMIN).status_code == 404


def test_only_owners_and_admins_change_members(client, students):
    team = _team(client, students)
    base = f"/projects/{team['project']}/members/{team['owner_id']}"
    assert client.patch(base, json={"role": "member"}, headers=_as(team["member"])).status_code == 403
    assert client.delete(base, headers=_as(team["member"])).status_code == 403
    assert client.patch(base, json={"role": "owner"}, headers=_as(_email("outsider"))).status_code == 403
    assert client.patch(base, json={"role": "admin"}, headers=_as(team["owner"])).status_code == 422


def test_an_owner_promotes_a_member_and_may_then_step_down(client, students):
    team = _team(client, students)
    url = f"/projects/{team['project']}/members/{team['member_id']}"
    promoted = client.patch(url, json={"role": "owner"}, headers=_as(team["owner"]))
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "owner" and promoted.json()["email"] == team["member"]

    own = f"/projects/{team['project']}/members/{team['owner_id']}"
    assert client.patch(own, json={"role": "member"}, headers=_as(team["owner"])).status_code == 200
    now = _members(client, team, team["member"])
    assert now[team["owner"]]["role"] == "member" and now[team["member"]]["role"] == "owner"
    assert client.patch(f"/projects/{team['project']}/members/{uuid.uuid4()}", json={"role": "owner"}, headers=ADMIN).status_code == 404


def test_the_last_owner_can_be_neither_demoted_nor_removed(client, students):
    team = _team(client, students)
    own = f"/projects/{team['project']}/members/{team['owner_id']}"
    demote = client.patch(own, json={"role": "member"}, headers=_as(team["owner"]))
    assert demote.status_code == 409 and demote.json()["error"] == "last_owner"
    remove = client.delete(own, headers=_as(team["owner"]))
    assert remove.status_code == 409 and remove.json()["error"] == "last_owner"
    # an admin is held to the same rule
    assert client.delete(own, headers=ADMIN).status_code == 409
    assert _members(client, team)[team["owner"]]["role"] == "owner"


def test_removing_a_member_ends_their_access(client, students):
    team = _team(client, students)
    url = f"/projects/{team['project']}/members/{team['member_id']}"
    removed = client.delete(url, headers=_as(team["owner"]))
    assert removed.status_code == 200 and removed.json() == {"user_id": team["member_id"], "removed": True}
    assert team["member"] not in _members(client, team)
    assert client.get(f"/projects/{team['project']}/members", headers=_as(team["member"])).status_code == 403
    assert client.delete(url, headers=_as(team["owner"])).status_code == 404


def test_an_admin_can_manage_any_projects_members(client, students):
    team = _team(client, students)
    url = f"/projects/{team['project']}/members/{team['member_id']}"
    assert client.patch(url, json={"role": "owner"}, headers=ADMIN).status_code == 200
    assert client.delete(url, headers=ADMIN).status_code == 200


def test_user_search_pages_and_filters(client, students):
    tag = uuid.uuid4().hex[:8]
    emails = [f"find-{tag}-{n}@uni.edu" for n in range(3)]
    for email in emails:
        client.get("/me", headers=_as(email))
    found = client.get("/admin/users", params={"q": f"FIND-{tag}"}, headers=ADMIN).json()
    assert {u["email"] for u in found["items"]} == set(emails)
    first = found["items"][0]
    assert set(first) >= {"id", "email", "display_name", "is_admin", "blocked", "disabled_at", "created_at", "max_concurrent_jobs"}
    assert first["blocked"] is False and first["is_admin"] is False

    first_page = client.get("/admin/users", params={"q": tag, "limit": 2}, headers=ADMIN).json()
    assert len(first_page["items"]) == 2 and first_page["next_cursor"]
    second_page = client.get("/admin/users", params={"q": tag, "limit": 2, "cursor": first_page["next_cursor"]}, headers=ADMIN).json()
    assert len(second_page["items"]) == 1 and second_page["next_cursor"] is None
    ids = [u["id"] for u in first_page["items"] + second_page["items"]]
    assert len(set(ids)) == 3
    assert client.get("/admin/users", params={"cursor": "junk"}, headers=ADMIN).status_code == 400

    victim = found["items"][0]
    client.post(f"/admin/users/{victim['id']}/block", headers=ADMIN)
    only_blocked = client.get("/admin/users", params={"q": tag, "blocked": "true"}, headers=ADMIN).json()
    assert [u["id"] for u in only_blocked["items"]] == [victim["id"]]
    not_blocked = client.get("/admin/users", params={"q": tag, "blocked": "false"}, headers=ADMIN).json()
    assert victim["id"] not in {u["id"] for u in not_blocked["items"]}
    client.post(f"/admin/users/{victim['id']}/unblock", headers=ADMIN)


def test_a_blocked_user_gets_403_everywhere_until_unblocked(client, students):
    email = _email("blocked")
    portal = Portal(client, email)
    project = portal.ensure_project()
    job = portal.submit_job(project_id=project)
    user_id = client.get("/me", headers=_as(email)).json()["id"]

    blocked = client.post(f"/admin/users/{user_id}/block", headers=ADMIN)
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["blocked"] is True and blocked.json()["disabled_at"]

    mine = _as(email)
    probes = [
        ("get", "/me"), ("get", "/projects"), ("get", "/jobs"), ("get", f"/jobs/{job['id']}"),
        ("get", f"/jobs/{job['id']}/logs"), ("get", "/environments"), ("get", "/me/usage"),
        ("post", f"/jobs/{job['id']}/cancel"), ("get", f"/projects/{project}/members"),
    ]
    for method, path in probes:
        resp = getattr(client, method)(path, headers=mine)
        assert resp.status_code == 403, (path, resp.status_code)
        assert resp.json()["error"] == "disabled"
    # a signed-in browser session is refused the same way
    cookie = {"Authorization": f"Bearer {issue_session(user_id)}"}
    assert client.get("/me", headers=cookie).status_code == 403

    again = client.post(f"/admin/users/{user_id}/block", headers=ADMIN)  # blocking twice is harmless
    assert again.status_code == 200
    assert client.post(f"/admin/users/{user_id}/unblock", headers=ADMIN).json()["blocked"] is False
    assert client.get("/me", headers=mine).status_code == 200
    assert client.get("/me", headers=cookie).status_code == 200


def test_block_rules(client, students):
    me = client.get("/me", headers=ADMIN).json()
    assert client.post(f"/admin/users/{me['id']}/block", headers=ADMIN).status_code == 409
    assert client.post(f"/admin/users/{uuid.uuid4()}/block", headers=ADMIN).status_code == 404
    assert client.post(f"/admin/users/{uuid.uuid4()}/unblock", headers=ADMIN).status_code == 404
    student = _as(_email("s"))
    assert client.get("/admin/users", headers=student).status_code == 403
    assert client.post(f"/admin/users/{me['id']}/block", headers=student).status_code == 403


def test_quota_read_and_write_for_users_and_projects(client, students):
    team = _team(client, students)
    one = client.get("/admin/quotas", params={"subject_type": "user", "subject_id": team["owner_id"]}, headers=ADMIN)
    assert one.status_code == 200, one.text
    item = one.json()["items"][0]
    assert item["subject_type"] == "user" and item["label"] == team["owner"]
    assert item["max_concurrent_jobs"] == settings.default_user_concurrency

    put = client.put(f"/admin/quotas/user/{team['owner_id']}", json={"max_concurrent_jobs": 7}, headers=ADMIN)
    assert put.status_code == 200, put.text
    assert put.json()["max_concurrent_jobs"] == 7 and put.json()["default"] is False
    got = client.get("/admin/quotas", params={"subject_type": "user", "subject_id": team["owner_id"]}, headers=ADMIN)
    assert got.json()["items"][0]["max_concurrent_jobs"] == 7

    project_put = client.put(f"/admin/quotas/project/{team['project']}", json={"max_concurrent_jobs": 3}, headers=ADMIN)
    assert project_put.json()["label"] and project_put.json()["max_concurrent_jobs"] == 3
    shown = client.get(f"/projects/{team['project']}/members", headers=_as(team["owner"])).json()
    assert shown["project"]["max_concurrent_jobs"] == 3

    listed = client.get("/admin/quotas", params={"subject_type": "project", "limit": 1}, headers=ADMIN).json()
    assert len(listed["items"]) == 1
    everyone = client.get("/admin/quotas", params={"limit": 200}, headers=ADMIN).json()
    assert everyone["items"]
    users_page = client.get("/admin/users", params={"q": team["owner"]}, headers=ADMIN).json()["items"][0]
    assert users_page["max_concurrent_jobs"] == 7


def test_quota_paging_walks_every_row_once(client, students):
    for _ in range(3):
        client.get("/me", headers=_as(_email("q")))
    seen: list[tuple[str, str]] = []
    cursor = None
    while True:
        params = {"limit": 2, **({"cursor": cursor} if cursor else {})}
        page = client.get("/admin/quotas", params=params, headers=ADMIN).json()
        seen += [(q["subject_type"], q["subject_id"]) for q in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) >= 3


def test_quota_input_is_checked(client, students):
    team = _team(client, students)
    uid = team["owner_id"]
    assert client.put(f"/admin/quotas/team/{uid}", json={"max_concurrent_jobs": 1}, headers=ADMIN).status_code == 400
    assert client.put(f"/admin/quotas/user/{uuid.uuid4()}", json={"max_concurrent_jobs": 1}, headers=ADMIN).status_code == 404
    assert client.put(f"/admin/quotas/user/{uid}", json={"max_concurrent_jobs": -1}, headers=ADMIN).status_code == 422
    assert client.put(f"/admin/quotas/user/{uid}", json={"max_concurrent_jobs": 1001}, headers=ADMIN).status_code == 422
    assert client.get("/admin/quotas", params={"subject_type": "team"}, headers=ADMIN).status_code == 400
    assert client.get("/admin/quotas", params={"subject_id": uid}, headers=ADMIN).status_code == 400
    assert client.get("/admin/quotas", params={"subject_type": "user", "subject_id": str(uuid.uuid4())}, headers=ADMIN).status_code == 404
    student = _as(_email("s"))
    assert client.put(f"/admin/quotas/user/{uid}", json={"max_concurrent_jobs": 1}, headers=student).status_code == 403


# --- Invites (Q4) --------------------------------------------------------------------


def test_an_invite_is_flagged_until_the_person_signs_in(client, students):
    team = _team(client, students)
    added = client.post(
        f"/projects/{team['project']}/members", json={"email": f"  New-{uuid.uuid4().hex[:6]}@UNI.edu "}, headers=_as(team["owner"])
    )
    assert added.status_code == 201 and added.json()["invited"] is True
    email = client.get(f"/admin/users?q={added.json()['user_id']}", headers=ADMIN)  # not an email: no match
    assert email.json()["items"] == []

    members = _members(client, team)
    invited_email = next(e for e, m in members.items() if m["user_id"] == added.json()["user_id"])
    assert invited_email == invited_email.lower() and invited_email == invited_email.strip()
    assert members[invited_email]["invited"] is True
    assert members[team["owner"]]["invited"] is False  # the owner has signed in

    assert client.get("/me", headers=_as(invited_email)).status_code == 200  # first sign-in claims it
    assert _members(client, team)[invited_email]["invited"] is False
    again = client.post(
        f"/projects/{team['project']}/members", json={"email": invited_email}, headers=_as(team["owner"])
    )
    assert again.status_code == 201 and again.json()["invited"] is False


def test_the_user_list_filters_by_invited(client, students):
    team = _team(client, students)
    owner_id = team["owner_id"]
    invited_id, signed_in_id = team["member_id"], owner_id

    def ids(flag: str) -> set[str]:
        resp = client.get(f"/admin/users?invited={flag}&limit=200", headers=ADMIN)
        assert resp.status_code == 200, resp.text
        assert all(item["invited"] is (flag == "1") for item in resp.json()["items"])
        return {item["id"] for item in resp.json()["items"]}

    assert invited_id in ids("1") and signed_in_id not in ids("1")
    assert signed_in_id in ids("0") and invited_id not in ids("0")
    listed = client.get("/admin/users?limit=200", headers=ADMIN).json()["items"]
    assert {u["id"]: u["invited"] for u in listed}[invited_id] is True


def test_a_local_sign_in_claims_an_invite(client, students):
    team = _team(client, students)
    assert _members(client, team)[team["member"]]["invited"] is True
    assert client.post("/auth/local", json={"email": team["member"], "name": "M"}).status_code == 200
    client.cookies.clear()
    assert _members(client, team)[team["member"]]["invited"] is False


def test_invites_must_match_the_allowed_domains(client, students, monkeypatch):
    team = _team(client, students)
    monkeypatch.setattr(settings, "oidc_allowed_email_domains", "uni.edu, @lab.edu")
    url = f"/projects/{team['project']}/members"
    bad = client.post(url, json={"email": f"x-{uuid.uuid4().hex[:6]}@gmail.com"}, headers=_as(team["owner"]))
    assert bad.status_code == 422 and bad.json()["error"] == "email_domain"
    assert "uni.edu" in bad.json()["message"] and "lab.edu" in bad.json()["message"]
    assert client.post(url, json={"email": "not-an-email"}, headers=_as(team["owner"])).status_code == 422
    ok = client.post(url, json={"email": f" Y-{uuid.uuid4().hex[:6]}@LAB.edu "}, headers=_as(team["owner"]))
    assert ok.status_code == 201 and ok.json()["invited"] is True
