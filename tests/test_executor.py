from __future__ import annotations

from pathlib import Path

from agent.executor import build_docker_run_cmd, container_name, is_registry_digest, resolve_image, CPU_IMAGE


def _cmd(**overrides) -> list[str]:
    defaults = {
        "attempt_id": "abc_def",
        "image": CPU_IMAGE,
        "input_dir": Path("/tmp/in"),
        "output_dir": Path("/tmp/out"),
        "cache_dir": Path("/tmp/cache"),
        "tmp_dir": Path("/tmp/jobtmp"),
        "entrypoint": "train.py",
        "args": ["--epochs", "1"],
        "cpu_cores": 2,
        "memory_mb": 1024,
        "want_gpu": False,
    }
    defaults.update(overrides)
    return build_docker_run_cmd(**defaults)


def test_container_name_is_lab_prefixed():
    assert container_name("abc_def").startswith("lab-")
    assert "_" not in container_name("abc_def")


def test_dev_digest_maps_to_cpu_image(monkeypatch):
    monkeypatch.delenv("LAB_IMAGE", raising=False)
    assert resolve_image("sha256:dev-pytorch-local") == CPU_IMAGE
    assert resolve_image("sha256:dev-tensorflow-local") == CPU_IMAGE
    assert resolve_image("pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime") == (
        "pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime"
    )
    # Builder must not docker pull "sha256" as a Hub repo name.
    assert not resolve_image("sha256:dev-tensorflow-local").startswith("sha256:")


def test_lab_image_override(monkeypatch):
    monkeypatch.setenv("LAB_IMAGE", "pytorch/pytorch@sha256:abc")
    assert resolve_image("sha256:dev-pytorch-local") == "pytorch/pytorch@sha256:abc"


def test_registry_digest_shape():
    digest = "192.168.68.10:5000/lab-env/abcd@sha256:" + ("ab" * 32)
    assert is_registry_digest(digest)
    assert not is_registry_digest("sha256:" + ("ab" * 32))
    assert not is_registry_digest("lab-env:abcd")


def test_build_docker_run_cmd_hardening():
    cmd = _cmd()
    joined = " ".join(cmd)
    assert cmd[:2] == ["docker", "run"]
    assert "--read-only" in cmd
    assert "--network" in cmd
    assert "none" in cmd
    assert "--user" in cmd
    assert "10000:10000" in cmd
    assert "--pids-limit" in cmd
    assert "--name" in cmd
    assert "lab-abc-def" in cmd
    assert "/input/train.py" in cmd
    assert "--gpus" not in cmd
    assert "docker.sock" not in joined
    assert "/var/run/docker.sock" not in joined


def test_gpu_flag_only_when_requested():
    cmd = _cmd(want_gpu=True)
    assert "--gpus" in cmd
    assert "all" in cmd
    assert "10000:10000" not in cmd
    cpu = _cmd(want_gpu=False)
    assert "--gpus" not in cpu
    assert "10000:10000" in cpu


def _silent_job(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "train.py").write_text("import time\nprint('working', flush=True)\ntime.sleep(60)\n")
    return src


def test_silent_job_still_times_out(tmp_path):
    import sys
    import threading
    import time

    from agent.executor import run_local_python

    lines = []
    start = time.time()
    code, category = run_local_python(
        python_bin=sys.executable, input_dir=_silent_job(tmp_path), output_dir=tmp_path / "out",
        entrypoint="train.py", args=[], timeout_seconds=1, stop=threading.Event(), on_line=lines.append,
    )
    assert (code, category) == (124, "timeout")
    assert time.time() - start < 5
    assert lines == ["working"]


def test_silent_job_can_be_cancelled(tmp_path):
    import sys
    import threading
    import time

    from agent.executor import run_local_python

    stop = threading.Event()
    threading.Timer(1.0, stop.set).start()
    start = time.time()
    _code, category = run_local_python(
        python_bin=sys.executable, input_dir=_silent_job(tmp_path), output_dir=tmp_path / "out",
        entrypoint="train.py", args=[], timeout_seconds=60, stop=stop, on_line=lambda _line: None,
    )
    assert category == "cancelled"
    assert time.time() - start < 8
