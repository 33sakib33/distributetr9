from __future__ import annotations

import uuid

from app.config import settings
from app.oidc import clear_discovery_cache
from app.session_tokens import issue_session, read_session


def test_auth_config(client):
    resp = client.get("/auth/config")
    assert resp.status_code == 200
    body = resp.json()
    assert body["local_login"] is True
    assert body["campus_login"] is False
    assert body["login_url"] is None


def test_dev_header_ignored_when_disabled(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_auth", False)
    resp = client.get("/me", headers={"X-Dev-User": "alice@uni.edu"})
    assert resp.status_code == 401
    assert "sign in" in resp.json()["message"].lower()


def test_session_cookie_authenticates(client, monkeypatch):
    created = client.get("/me", headers={"X-Dev-User": "sam@uni.edu"})
    assert created.status_code == 200
    token = issue_session(created.json()["id"])
    monkeypatch.setattr(settings, "dev_auth", False)
    client.cookies.set("lab_session", token)
    try:
        resp = client.get("/me")
        assert resp.status_code == 200
        assert resp.json()["email"] == "sam@uni.edu"
        assert read_session(token) == created.json()["id"]
    finally:
        client.cookies.clear()


def _oidc_login(client, monkeypatch, email: str, name: str) -> dict:
    monkeypatch.setattr(settings, "oidc_dev", True)
    monkeypatch.setattr(settings, "oidc_issuer", "http://testserver/oidc")
    monkeypatch.setattr(settings, "oidc_redirect_uri", "http://testserver/auth/callback")
    monkeypatch.setattr(settings, "portal_base_url", "http://portal.test")
    clear_discovery_cache()
    client.cookies.clear()

    start = client.get("/auth/login?next=http://portal.test/", follow_redirects=False)
    assert start.status_code == 302
    loc = start.headers["location"]
    posted = client.post(
        loc,
        data={"email": email, "name": name},
        follow_redirects=False,
    )
    assert posted.status_code == 302
    callback = posted.headers["location"]
    done = client.get(callback, follow_redirects=False)
    assert done.status_code == 302
    assert "ticket=" in done.headers["location"]
    me = client.get("/me")
    assert me.status_code == 200
    return me.json()


def test_oidc_login_creates_user(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_auth", False)
    try:
        me = _oidc_login(client, monkeypatch, "carol@uni.edu", "Carol")
        assert me["email"] == "carol@uni.edu"
        assert me["display_name"] == "Carol"
        assert me["is_admin"] is False
    finally:
        client.cookies.clear()


def test_oidc_admin_email(client, monkeypatch):
    monkeypatch.setattr(settings, "dev_auth", False)
    try:
        me = _oidc_login(client, monkeypatch, "admin@uni.edu", "Admin")
        assert me["is_admin"] is True
    finally:
        client.cookies.clear()


def test_invite_claimed_on_first_oidc_login(client, monkeypatch):
    owner = {"X-Dev-User": "owner@uni.edu"}
    name = "shared-" + uuid.uuid4().hex[:8]
    proj = client.post("/projects", json={"name": name}, headers=owner)
    assert proj.status_code == 201
    pid = proj.json()["id"]
    added = client.post(
        f"/projects/{pid}/members",
        json={"email": "pat@uni.edu", "role": "member"},
        headers=owner,
    )
    assert added.status_code == 201
    monkeypatch.setattr(settings, "dev_auth", False)
    try:
        me = _oidc_login(client, monkeypatch, "pat@uni.edu", "Pat")
        assert me["email"] == "pat@uni.edu"
        listed = client.get("/projects")
        assert listed.status_code == 200
        names = [p["name"] for p in listed.json()["projects"]]
        assert name in names
    finally:
        client.cookies.clear()


def test_local_email_login(client):
    resp = client.post("/auth/local", json={"email": "lee@uni.edu", "name": "Lee"})
    assert resp.status_code == 200
    client.cookies.set("lab_session", resp.json()["token"])
    try:
        me = client.get("/me")
        assert me.status_code == 200
        assert me.json()["email"] == "lee@uni.edu"
        assert me.json()["display_name"] == "Lee"
    finally:
        client.cookies.clear()
