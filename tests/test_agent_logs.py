"""Agent logs on demand (B13): the agent's log file and upload, and the server
side of request -> heartbeat -> upload -> read."""
from __future__ import annotations

import io
import re
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agent import agent as agent_mod
from agent.agent import LogFile, LogTee, install_log_tee, read_log_tail
from app.agent_logs import MAX_UPLOAD_BYTES
from app.models import Node
from harness.client import FakeAgent
from harness.jobs import Portal

# --- agent: the log file ------------------------------------------------------


def _tee(tmp_path: Path, max_bytes: int = 2 * 1024 * 1024) -> tuple[LogTee, io.StringIO, Path]:
    path = tmp_path / "state" / "agent.log"
    stream = io.StringIO()
    return LogTee(stream, LogFile(path, max_bytes)), stream, path


def test_tee_passes_output_through_and_writes_timestamped_lines(tmp_path):
    tee, stream, path = _tee(tmp_path)
    print("hello", 1, file=tee, flush=True)
    tee.write("half a li")
    tee.write("ne\nnext\n")
    assert stream.getvalue() == "hello 1\nhalf a line\nnext\n"
    lines = path.read_text().splitlines()
    assert [line.split(" ", 1)[1] for line in lines] == ["hello 1", "half a line", "next"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", lines[0].split(" ", 1)[0])


def test_an_unfinished_line_waits_for_its_newline(tmp_path):
    tee, _stream, path = _tee(tmp_path)
    tee.write("no newline yet")
    assert not path.exists()


def test_log_rotates_at_the_size_limit_and_keeps_one_old_file(tmp_path):
    tee, _stream, path = _tee(tmp_path, max_bytes=2000)
    for i in range(200):
        print("line", i, "x" * 20, file=tee)
    old = path.with_name("agent.log.1")
    assert old.exists() and not path.with_name("agent.log.2").exists()
    assert path.stat().st_size <= 2000 and old.stat().st_size <= 2000
    tail = read_log_tail(path, lines=10).splitlines()
    assert tail[-1].endswith("line 199 " + "x" * 20)
    assert len(tail) == 10


def test_tail_reaches_back_into_the_rotated_file(tmp_path):
    tee, _stream, path = _tee(tmp_path, max_bytes=600)
    for i in range(30):
        print("n", i, file=tee)
    in_current = len(path.read_text().splitlines())
    assert in_current < 30
    tail = read_log_tail(path, lines=in_current + 3).splitlines()
    assert len(tail) == in_current + 3
    assert tail[-1].endswith("n 29")


def test_tail_of_a_missing_log_is_empty(tmp_path):
    assert read_log_tail(tmp_path / "nope.log") == ""


def test_a_log_file_that_cannot_be_written_never_breaks_printing(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    stream = io.StringIO()
    tee = LogTee(stream, LogFile(blocker / "agent.log"))
    print("still works", file=tee)
    assert stream.getvalue() == "still works\n"


def test_install_tee_wraps_stdout_and_stderr_once(tmp_path, monkeypatch):
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    path = tmp_path / "agent.log"
    install_log_tee(path)
    first = sys.stdout
    install_log_tee(path)
    assert sys.stdout is first
    print("to out")
    print("to err", file=sys.stderr)
    assert out.getvalue() == "to out\n" and err.getvalue() == "to err\n"
    assert [line.split(" ", 1)[1] for line in path.read_text().splitlines()] == ["to out", "to err"]


# --- agent: answering a request ------------------------------------------------


def _agent(tmp_path):
    return agent_mod.Agent("http://x", tmp_path / "state" / "agent-state.json", sys.executable, "python")


def test_log_path_sits_next_to_the_state_file(tmp_path):
    assert _agent(tmp_path).log_path == tmp_path / "state" / "agent.log"


def test_agent_uploads_the_log_tail_when_a_heartbeat_asks(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    agent.state.update(node_token="t", node_id="n", boot_session_id="b")
    agent.log_path.parent.mkdir(parents=True)
    agent.log_path.write_text("".join(f"2026-01-01T00:00:00Z line {i}\n" for i in range(800)))
    sent = []
    done = threading.Event()

    def fake_bytes(method, url, data=None, headers=None, **k):
        sent.append((method, url, data, headers))
        done.set()
        return 200, b"{}"

    monkeypatch.setattr(agent_mod, "http_bytes", fake_bytes)
    agent.handle_requests({"command": "continue", "requests": []})
    agent.handle_requests(None)
    agent.handle_requests({"requests": ["something_else"]})
    assert not sent
    agent.handle_requests({"requests": ["upload_logs"]})
    assert done.wait(5)
    method, url, data, headers = sent[0]
    assert (method, url) == ("POST", "http://x/agent/logs-upload")
    lines = data.decode().splitlines()
    assert len(lines) == 500 and lines[-1].endswith("line 799")
    assert headers["Authorization"] == "Bearer t" and headers["Content-Type"].startswith("text/plain")


def test_only_one_upload_runs_at_a_time(tmp_path, monkeypatch):
    agent = _agent(tmp_path)
    agent.state.update(node_token="t", node_id="n", boot_session_id="b")
    gate = threading.Event()
    calls = []

    def slow(method, url, data=None, headers=None, **k):
        calls.append(url)
        gate.wait(5)
        return 200, b"{}"

    monkeypatch.setattr(agent_mod, "http_bytes", slow)
    agent.handle_requests({"requests": ["upload_logs"]})
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    agent.handle_requests({"requests": ["upload_logs"]})
    gate.set()
    time.sleep(0.2)
    assert len(calls) == 1


def test_a_failed_upload_is_logged_not_raised(tmp_path, monkeypatch, capsys):
    import urllib.error

    agent = _agent(tmp_path)
    agent.state.update(node_token="t", node_id="n", boot_session_id="b")

    def boom(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(agent_mod, "http_bytes", boom)
    agent.upload_logs()
    assert "log upload failed" in capsys.readouterr().out


# --- server -------------------------------------------------------------------


def _node(client) -> FakeAgent:
    agent = FakeAgent(client, hostname=f"logs-{uuid.uuid4().hex[:8]}")
    agent.register()
    agent.heartbeat()
    return agent


def _get(client, agent: FakeAgent) -> dict:
    resp = client.get(f"/nodes/{agent.node_id}/logs", headers=Portal(client).headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ask(client, agent: FakeAgent):
    return client.post(f"/nodes/{agent.node_id}/logs/request", headers=Portal(client).headers)


def test_request_then_heartbeat_then_upload_then_read(client):
    agent = _node(client)
    assert agent.heartbeat()["requests"] == []
    assert _get(client, agent) == {"requested_at": None, "uploaded_at": None, "text": None}

    asked = _ask(client, agent)
    assert asked.status_code == 200 and asked.json()["requested_at"]
    assert agent.heartbeat()["requests"] == ["upload_logs"]
    assert agent.heartbeat()["requests"] == []  # once

    pending = _get(client, agent)
    assert pending["requested_at"] and pending["uploaded_at"] is None

    up = agent.upload_logs("2026-01-01 started\n2026-01-01 claimed job\n")
    assert up["status_code"] == 200, up
    assert up["body"]["object_key"].startswith(f"nodes/{agent.node_id}/agent-log-")
    got = _get(client, agent)
    assert got["text"] == "2026-01-01 started\n2026-01-01 claimed job\n"
    assert got["uploaded_at"] >= got["requested_at"]
    assert agent.heartbeat()["requests"] == []


def test_a_new_request_after_an_upload_goes_out_again_and_replaces_the_text(client):
    from app.storage import storage

    agent = _node(client)
    _ask(client, agent)
    agent.heartbeat()
    first_key = agent.upload_logs("first\n")["body"]["object_key"]
    assert storage.exists(first_key)
    time.sleep(1.1)  # keys carry whole seconds
    _ask(client, agent)
    assert agent.heartbeat()["requests"] == ["upload_logs"]
    second_key = agent.upload_logs("second\n")["body"]["object_key"]
    assert second_key != first_key
    assert _get(client, agent)["text"] == "second\n"
    assert not storage.exists(first_key)


async def test_a_request_the_agent_never_answered_is_sent_again_later(client):
    agent = _node(client)
    _ask(client, agent)
    assert agent.heartbeat()["requests"] == ["upload_logs"]
    assert agent.heartbeat()["requests"] == []

    import os

    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as session:
            await session.execute(
                update(Node)
                .where(Node.id == uuid.UUID(agent.node_id))
                .values(logs_request_sent_at=Node.logs_request_sent_at - timedelta(minutes=3))
            )
            await session.commit()
    finally:
        await engine.dispose()
    assert agent.heartbeat()["requests"] == ["upload_logs"]


def test_oversized_uploads_are_refused(client):
    agent = _node(client)
    big = agent.upload_logs("x" * (MAX_UPLOAD_BYTES + 1))
    assert big["status_code"] == 413
    assert _get(client, agent)["text"] is None


def test_logs_endpoints_are_admin_only_and_404_for_unknown_nodes(client, monkeypatch):
    from app.config import settings

    agent = _node(client)
    ghost = uuid.uuid4()
    admin = Portal(client).headers
    assert client.get(f"/nodes/{ghost}/logs", headers=admin).status_code == 404
    assert client.post(f"/nodes/{ghost}/logs/request", headers=admin).status_code == 404
    monkeypatch.setattr(settings, "dev_auth_admin", False)
    stranger = {"X-Dev-User": f"stranger-{uuid.uuid4().hex[:6]}@uni.edu"}
    assert client.get(f"/nodes/{agent.node_id}/logs", headers=stranger).status_code == 403
    assert client.post(f"/nodes/{agent.node_id}/logs/request", headers=stranger).status_code == 403


def test_agent_upload_needs_a_node_token(client):
    resp = client.post("/agent/logs-upload", content=b"hi")
    assert resp.status_code == 401


@pytest.mark.parametrize("text", ["", "ünïcode ✓\n"])
def test_empty_and_unicode_logs_round_trip(client, text):
    agent = _node(client)
    _ask(client, agent)
    agent.heartbeat()
    assert agent.upload_logs(text)["status_code"] == 200
    assert _get(client, agent)["text"] == text
