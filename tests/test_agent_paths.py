from __future__ import annotations

import os
from pathlib import Path

from agent.agent import resolve_attempt_root


def test_attempt_root_skips_unwritable(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    varlab = tmp_path / "varlab"
    varlab.mkdir()
    varlab.chmod(0o500)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("LAB_ATTEMPT_ROOT", str(blocked))
    root = resolve_attempt_root(system_root=varlab)
    assert root == home / ".distributetr9" / "attempts"
    assert root.is_dir()
    (root / "probe.txt").write_text("ok")


def test_attempt_root_uses_writable_env(tmp_path, monkeypatch):
    wanted = tmp_path / "attempts"
    monkeypatch.setenv("LAB_ATTEMPT_ROOT", str(wanted))
    monkeypatch.delenv("HOME", raising=False)
    assert resolve_attempt_root() == wanted
    assert wanted.is_dir()
    wanted.chmod(0o700)
