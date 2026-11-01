"""Why the agent restarted (B5): the decision is a pure function; the agent
reads its inputs from files and the server stores what it is told."""
from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from agent import agent as agent_mod
from agent.agent import Agent, decide_restart, read_boot_id, read_last_exit
from agent.updater import EXIT_UPDATED
from app.models import NodeEvent
from harness.client import FakeAgent

NOW = 1_700_000_000.0
REGISTERED = NOW - 1000


def decide(**overrides) -> dict:
    inputs = {
        "previously_registered": True,
        "boot_id": "boot-1",
        "prev_boot_id": "boot-1",
        "last_register_at": REGISTERED,
        "last_exit": None,
        "crash": None,
        "now": NOW,
    }
    inputs.update(overrides)
    return decide_restart(**inputs)


def test_first_start_when_never_registered():
    out = decide(previously_registered=False, prev_boot_id=None, last_register_at=None, last_exit=(1, NOW - 5))
    assert out == {"reason": "first_start", "exit_code": None, "detail": None, "at": None}


def test_reboot_when_the_boot_id_changed():
    out = decide(boot_id="boot-2", last_exit=(0, NOW - 5), crash=(NOW - 5, "trace"))
    assert out["reason"] == "reboot"
    assert out["exit_code"] is None


def test_update_when_the_previous_exit_was_75():
    out = decide(last_exit=(EXIT_UPDATED, NOW - 30))
    assert out["reason"] == "update"
    assert out["exit_code"] == 75
    assert out["at"] == "2023-11-14T22:12:50Z"


def test_crash_when_the_crash_file_is_newer_than_the_last_register():
    out = decide(last_exit=(1, NOW - 4), crash=(NOW - 5, "Traceback\nKeyError: 'x'\n"))
    assert out["reason"] == "crash"
    assert out["exit_code"] == 1
    assert "KeyError" in out["detail"]


def test_old_crash_file_is_not_a_crash():
    out = decide(crash=(REGISTERED - 10, "stale trace"))
    assert out["reason"] == "manual"


def test_manual_otherwise():
    assert decide()["reason"] == "manual"
    out = decide(last_exit=(143, NOW - 3))
    assert (out["reason"], out["exit_code"]) == ("manual", 143)


def test_exit_older_than_the_last_register_is_ignored():
    out = decide(last_exit=(EXIT_UPDATED, REGISTERED - 60))
    assert out["reason"] == "manual"
    assert out["exit_code"] is None


def test_missing_boot_id_never_means_reboot():
    assert decide(boot_id=None)["reason"] == "manual"
    assert decide(prev_boot_id=None)["reason"] == "manual"


def test_parsers_tolerate_missing_and_garbled_files(tmp_path):
    assert read_boot_id(tmp_path / "nope") is None
    assert read_last_exit(tmp_path / "nope") is None
    garbled = tmp_path / "last_exit"
    garbled.write_text("oops")
    assert read_last_exit(garbled) is None
    garbled.write_text("75 1700000000\n")
    assert read_last_exit(garbled) == (75, 1700000000.0)


def make_agent(tmp_path, **state) -> Agent:
    state_path = tmp_path / "state" / "agent-state.json"
    state_path.parent.mkdir()
    agent = Agent("http://127.0.0.1:1", state_path, "python3", "python")
    agent.state.update(state)
    agent.release_root = tmp_path / "install"
    agent.release_root.mkdir()
    return agent


def test_agent_reads_launcher_and_crash_files(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "read_boot_id", lambda: "boot-1")
    agent = make_agent(tmp_path, node_id="n1", boot_id="boot-1", last_register_at=1.0)
    (agent.release_root / "last_exit").write_text("75 %d\n" % int(NOW))
    assert agent.restart_info()["reason"] == "update"

    (agent.release_root / "last_exit").write_text("1 %d\n" % int(NOW))
    agent.crash_path.write_text("Traceback\nValueError: boom\n")
    info = agent.restart_info()
    assert info["reason"] == "crash" and "ValueError: boom" in info["detail"]

    agent.restart_reported = True  # a second register in the same process says nothing
    assert agent.restart_info() is None


def test_uncaught_exception_leaves_the_last_40_trace_lines(tmp_path):
    agent = make_agent(tmp_path)

    def die() -> None:
        raise RuntimeError("\n".join("line %d" % i for i in range(60)))

    agent.run_forever = die  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        agent.run_logged()
    lines = agent.crash_path.read_text().splitlines()
    assert len(lines) == 40
    assert lines[-1] == "line 59"


def test_exit_requests_are_not_crashes(tmp_path):
    agent = make_agent(tmp_path)

    def update_exit() -> None:
        raise SystemExit(EXIT_UPDATED)

    agent.run_forever = update_exit  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        agent.run_logged()
    assert not agent.crash_path.exists()


def _nodes(client) -> dict:
    resp = client.get("/nodes", headers={"X-Dev-User": "admin@uni.edu"})
    assert resp.status_code == 200, resp.text
    return {n["hostname"]: n for n in resp.json()["nodes"]}


async def _event_count(node_id: str) -> int:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    try:
        async with AsyncSession(engine) as session:
            return await session.scalar(
                select(func.count()).select_from(NodeEvent).where(NodeEvent.node_id == uuid.UUID(node_id))
            )
    finally:
        await engine.dispose()


async def test_register_stores_the_restart_reason(client):
    host = f"restart-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(client, hostname=host)
    agent.register(last_restart={"reason": "first_start", "exit_code": None, "detail": None, "at": None})
    assert _nodes(client)[host]["last_restart"]["reason"] == "first_start"

    crash = {"reason": "crash", "exit_code": 1, "detail": "Traceback\nKeyError", "at": "2026-11-01T20:30:00Z"}
    agent.register(last_restart=crash)
    stored = _nodes(client)[host]["last_restart"]
    assert stored == crash

    agent.register()  # a register that reports nothing keeps what is known
    assert _nodes(client)[host]["last_restart"] == crash
    assert await _event_count(agent.node_id) == 2


async def test_unknown_reason_is_stored_as_manual(client):
    host = f"restart-{uuid.uuid4().hex[:8]}"
    agent = FakeAgent(client, hostname=host)
    agent.register(last_restart={"reason": "alien abduction"})
    assert _nodes(client)[host]["last_restart"]["reason"] == "manual"


def test_nodes_without_a_report_show_null(client):
    host = f"restart-{uuid.uuid4().hex[:8]}"
    FakeAgent(client, hostname=host).register()
    assert _nodes(client)[host]["last_restart"] is None


def test_launcher_records_every_exit(tmp_path):
    import subprocess
    from pathlib import Path

    launcher = Path(agent_mod.__file__).with_name("launcher.sh")
    root = tmp_path / "lab-agent"
    (root / "current").mkdir(parents=True)
    # One run that asks for an update, then one that exits for good via a stop file.
    (root / "current" / "run.sh").write_text(
        'if [ -e "$LAB_AGENT_ROOT/stop" ]; then exit 3; fi\ntouch "$LAB_AGENT_ROOT/stop"; exit 75\n'
    )
    script = tmp_path / "once.sh"
    # Run the launcher loop until the second exit, then kill it.
    script.write_text(f'bash {launcher} & pid=$!; sleep 3; kill $pid 2>/dev/null; wait $pid 2>/dev/null; true\n')
    env = {**os.environ, "LAB_AGENT_ROOT": str(root), "LAB_AGENT_RESTART_DELAY": "30"}
    subprocess.run(["bash", str(script)], env=env, check=True, timeout=30)
    code, stamp = (root / "last_exit").read_text().split()
    assert code == "3" and int(stamp) > 1_700_000_000
