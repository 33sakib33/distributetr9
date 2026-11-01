"""Rooms and labels (B16): the agent's config names the room, an admin can override it."""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agent import agent as agent_mod
from agent.agent import Agent
from harness.client import FakeAgent

ADMIN = {"X-Dev-User": "admin@uni.edu"}
ROOT = Path(__file__).resolve().parents[1]


def _node(client, host: str, room: str | None = None) -> FakeAgent:
    agent = FakeAgent(client, hostname=host)
    agent.register(room=room)
    agent.heartbeat()
    return agent


def _fleet(client, **params) -> dict[str, dict]:
    resp = client.get("/nodes", params=params, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {n["hostname"]: n for n in resp.json()["nodes"]}


def _meta(client, agent: FakeAgent, **body):
    return client.post(f"/nodes/{agent.node_id}/meta", json=body, headers=ADMIN)


def test_a_node_has_no_room_or_labels_until_one_is_set(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    _node(client, host)
    node = _fleet(client)[host]
    assert node["room"] is None and node["room_source"] is None and node["labels"] == []


def test_the_agent_names_its_room_at_register(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    agent = _node(client, host, room="  Lab 204 ")
    node = _fleet(client)[host]
    assert node["room"] == "Lab 204" and node["room_source"] == "agent"

    agent.register(room="Lab 205")  # moved the PC and changed the config: the agent's word stands
    assert _fleet(client)[host]["room"] == "Lab 205"

    agent.register()  # a register without a room changes nothing
    assert _fleet(client)[host]["room"] == "Lab 205"


def test_an_overlong_room_is_cut_and_a_blank_one_ignored(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    agent = _node(client, host, room="x" * 200)
    assert _fleet(client)[host]["room"] == "x" * 64
    agent.register(room="   ")
    assert _fleet(client)[host]["room"] == "x" * 64


def test_an_admin_set_room_is_not_overwritten_by_the_agent(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    agent = _node(client, host, room="Lab 204")
    resp = _meta(client, agent, room="Server room")
    assert resp.status_code == 200, resp.text
    assert resp.json()["room"] == "Server room" and resp.json()["room_source"] == "admin"

    agent.register(room="Lab 204")
    node = _fleet(client)[host]
    assert node["room"] == "Server room" and node["room_source"] == "admin"

    # clearing the room hands it back to the agent
    cleared = _meta(client, agent, room="")
    assert cleared.json()["room"] is None and cleared.json()["room_source"] is None
    agent.register(room="Lab 204")
    node = _fleet(client)[host]
    assert node["room"] == "Lab 204" and node["room_source"] == "agent"


def test_a_node_without_a_room_takes_the_agents(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    agent = _node(client, host)
    _meta(client, agent, labels=["gtx-1660"])
    agent.register(room="Lab 101")
    assert _fleet(client)[host]["room"] == "Lab 101"


def test_labels_are_tidied_and_independent_of_the_room(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    agent = _node(client, host, room="Lab 204")
    resp = _meta(client, agent, labels=[" gtx-1660 ", "teaching", "gtx-1660", ""])
    assert resp.status_code == 200, resp.text
    assert resp.json()["labels"] == ["gtx-1660", "teaching"]
    assert resp.json()["room"] == "Lab 204"  # a field left out is not touched

    assert _meta(client, agent, room="Lab 300").json()["labels"] == ["gtx-1660", "teaching"]
    assert _meta(client, agent, labels=[]).json()["labels"] == []
    assert _fleet(client)[host]["labels"] == []


def test_meta_rejects_bad_values_and_unknown_nodes(client):
    agent = _node(client, f"room-{uuid.uuid4().hex[:8]}")
    assert _meta(client, agent, room="r" * 65).status_code == 422
    assert _meta(client, agent, labels=["l" * 33]).status_code == 422
    assert _meta(client, agent, labels=[f"l{i}" for i in range(21)]).status_code == 422
    assert _meta(client, agent, labels="gpu").status_code == 422
    missing = client.post(f"/nodes/{uuid.uuid4()}/meta", json={"room": "x"}, headers=ADMIN)
    assert missing.status_code == 404


def test_meta_is_admin_only(client, monkeypatch):
    from app.config import settings

    agent = _node(client, f"room-{uuid.uuid4().hex[:8]}")
    client.get("/me", headers=ADMIN)
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    student = {"X-Dev-User": f"student-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.post(f"/nodes/{agent.node_id}/meta", json={"room": "x"}, headers=student).status_code == 403


def test_the_room_filter_is_exact_and_ignores_case(client):
    tag = uuid.uuid4().hex[:6]
    a, b, c = f"room-a-{tag}", f"room-b-{tag}", f"room-c-{tag}"
    _node(client, a, room=f"North {tag}")
    _node(client, b, room=f"north {tag}")
    _node(client, c, room=f"South {tag}")
    _node(client, f"room-d-{tag}")

    north = _fleet(client, room=f"NORTH {tag}")
    assert set(north) == {a, b}
    assert set(_fleet(client, room="North")).isdisjoint({a, b, c})  # not a prefix match
    assert {a, b, c} <= set(_fleet(client))
    payload = client.get("/nodes", params={"room": f"south {tag}"}, headers=ADMIN).json()
    assert [n["hostname"] for n in payload["nodes"]] == [c]
    assert f"South {tag}" in payload["rooms"] and f"North {tag}" in payload["rooms"]  # rooms ignores the filter
    assert _fleet(client, room="")  # an empty filter means everything


def test_forgotten_nodes_leave_the_room_list(client):
    host = f"room-{uuid.uuid4().hex[:8]}"
    room = f"Gone {uuid.uuid4().hex[:6]}"
    agent = _node(client, host, room=room)
    assert room in client.get("/nodes", headers=ADMIN).json()["rooms"]
    client.post(f"/nodes/{agent.node_id}/forget", headers=ADMIN)
    assert room not in client.get("/nodes", headers=ADMIN).json()["rooms"]
    assert host not in _fleet(client, room=room)
    assert host in _fleet(client, room=room, include_decommissioned=1)


# --- The agent and the installer ----------------------------------------------------


def _registering_agent(tmp_path, monkeypatch, **kwargs) -> tuple[Agent, list[dict]]:
    sent: list[dict] = []

    def fake_http(method, url, body=None, headers=None, **_kw):
        sent.append(body)
        return 200, {"node_id": "n1", "node_token": "t", "boot_session_id": "b"}

    caps = {
        "agent_healthy": True, "gpu_desktop_busy": False, "compatibility_class": "cpu", "gpu_model": "none",
        "vram_total_mb": 0, "vram_allocatable_mb": 0, "driver_version": "x", "compute_capability": "0.0",
        "cuda_preflight_ok": True, "cpu_cores": 2, "ram_mb": 1024, "disk_free_bytes": 1,
        "windows_version": "x", "wsl_version": "x",
    }
    monkeypatch.setattr(agent_mod, "http_json", fake_http)
    monkeypatch.setattr(agent_mod, "agent_register_fields", lambda **_kw: dict(caps))
    state = tmp_path / "state" / "agent-state.json"
    state.parent.mkdir()
    agent = Agent("http://127.0.0.1:1", state, "python3", "python", **kwargs)
    agent.release_root = tmp_path / "install"
    agent.release_root.mkdir()
    return agent, sent


def test_the_agent_sends_the_room_from_its_config(tmp_path, monkeypatch):
    agent, sent = _registering_agent(tmp_path, monkeypatch, room=" Lab 204 ")
    agent.register()
    assert sent[0]["room"] == "Lab 204"


@pytest.mark.parametrize("room", [None, "", "   "])
def test_the_agent_omits_the_room_when_none_is_configured(tmp_path, monkeypatch, room):
    agent, sent = _registering_agent(tmp_path, monkeypatch, room=room)
    agent.register()
    assert "room" not in sent[0]


def test_the_installer_refuses_a_room_that_would_break_config_json():
    for bad in ('say "hi"', "a\\b", "", "x" * 65, "-leading"):
        if not bad:
            continue
        run = subprocess.run(
            ["bash", str(ROOT / "scripts" / "setup-lab-node.sh"), "--api", "http://127.0.0.1:1", "--room", bad],
            capture_output=True, text=True,
        )
        assert run.returncode == 2, (bad, run.stderr)
        assert "room must be" in run.stderr


def test_the_installer_and_bootstrap_take_a_room():
    script = (ROOT / "scripts" / "setup-lab-node.sh").read_text()
    assert "--room) ROOM=" in script and '"room' in script
    bootstrap = (ROOT / "supervisor" / "bootstrap.ps1").read_text()
    assert '[string]$Room = ""' in bootstrap
    assert '$installer += @("--room", $Room)' in bootstrap


def test_a_config_rendered_by_the_installer_is_valid_json_and_keeps_the_room(tmp_path):
    """Run the installer's config block on its own (the rest needs root and Docker)."""
    script = (ROOT / "scripts" / "setup-lab-node.sh").read_text()
    start = script.index("CONFIG_JSON=/etc/lab-agent/config.json")
    end = script.index("chmod 644 /etc/lab-agent/config.json")
    block = script[start:end].replace("CONFIG_JSON=/etc/lab-agent/config.json", 'CONFIG_JSON="$OUT"')

    def render(out: Path, room: str) -> dict:
        run = subprocess.run(
            ["bash", "-c", f'API=http://x EXECUTOR=docker ROLE=train BUNDLE_KEY=null ROOM="$1" OUT="$2"\n{block}',
             "_", room, str(out)],
            capture_output=True, text=True,
        )
        assert run.returncode == 0, run.stderr
        return json.loads(out.read_text())

    out = tmp_path / "config.json"
    assert "room" not in render(out, "")
    cfg = render(out, "Lab 204")
    assert cfg["room"] == "Lab 204" and cfg["api_base"] == "http://x" and cfg["role"] == "train"
    assert render(out, "")["room"] == "Lab 204"  # a re-run without --room keeps it
    assert render(out, "Lab 9")["room"] == "Lab 9"
