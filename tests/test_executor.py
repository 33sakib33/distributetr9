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


def test_cuda_check_needs_a_matching_gpu_on_the_builder():
    from agent.executor import should_check_cuda

    check = lambda **kw: should_check_cuda(**{"need_cuda": True, "has_gpu": True, **kw})  # noqa: E731
    assert check(build_class="turing-6g", builder_class="turing-6g")
    assert not check(build_class="turing-6g", builder_class="ada-12g")
    assert not check(build_class="turing-6g", builder_class="cpu")
    assert not check(build_class="turing-6g", builder_class="turing-6g", has_gpu=False)
    assert not check(build_class="turing-6g", builder_class=None)
    assert not check(build_class="cpu", builder_class="cpu", need_cuda=False)


def _fake_docker(monkeypatch, calls: list[list[str]]):
    import subprocess

    from agent import executor

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = "numpy==1.26.4\n" if cmd[:2] == ["docker", "run"] else ""
        if cmd[:3] == ["docker", "image", "inspect"] and "--format" in cmd:
            out = "reg:5000/lab-env/abc@sha256:" + "ab" * 32 + "\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    return executor


def test_build_without_the_gpu_skips_the_cuda_check(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    executor = _fake_docker(monkeypatch, calls)
    monkeypatch.setattr(executor, "nvidia_smi_ok", lambda: True)
    result = executor.build_custom_image(
        base_image="pytorch/pytorch:2.5.1",
        image_tag="reg:5000/lab-env/abc",
        requirements="numpy==1.26.4\n",
        need_cuda=True,
        work_dir=tmp_path,
        build_class="turing-6g",
        builder_class="ada-12g",
    )
    assert result["ok"], result
    assert "cuda check skipped: builder has no turing-6g GPU" in result["log"]
    test_run = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--gpus" not in test_run
    assert "assert torch.cuda.is_available()" not in test_run[-1]
    assert "import_module('numpy')" in test_run[-1]  # the import test still runs


def test_build_on_a_matching_gpu_checks_cuda(monkeypatch, tmp_path):
    calls: list[list[str]] = []
    executor = _fake_docker(monkeypatch, calls)
    monkeypatch.setattr(executor, "nvidia_smi_ok", lambda: True)
    result = executor.build_custom_image(
        base_image="pytorch/pytorch:2.5.1",
        image_tag="reg:5000/lab-env/abc",
        requirements="numpy==1.26.4\n",
        need_cuda=True,
        work_dir=tmp_path,
        build_class="turing-6g",
        builder_class="turing-6g",
    )
    assert result["ok"], result
    assert "cuda check skipped" not in result["log"]
    test_run = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "--gpus" in test_run
    assert "assert torch.cuda.is_available()" in test_run[-1]


def test_split_ref_handles_registry_ports_and_digests():
    from agent.executor import normalize_ref, split_ref

    assert split_ref("reg:5000/lab/x:1.0") == ("reg:5000/lab/x", ":1.0")
    assert split_ref("reg:5000/lab/x") == ("reg:5000/lab/x", "")
    assert split_ref("ubuntu:22.04") == ("ubuntu", ":22.04")
    assert split_ref("reg:5000/lab/x@sha256:" + "a" * 64) == ("reg:5000/lab/x", "@sha256:" + "a" * 64)
    assert normalize_ref("reg:5000/lab-env/abc") == "reg:5000/lab-env/abc:latest"


def test_mirror_digest_is_the_one_for_the_pushed_repository():
    from agent.executor import pick_repo_digest

    tag = "reg:5000/lab/python-cpu:3.12.7-slim"
    mine = "reg:5000/lab/python-cpu@sha256:" + "ab" * 32
    hub = "python@sha256:" + "cd" * 32
    # A pulled image lists the digest it came from first; index 0 would be wrong.
    assert pick_repo_digest([hub, mine], tag) == mine
    assert pick_repo_digest([hub], tag) is None
    assert pick_repo_digest([], tag) is None


def test_mirror_image_pulls_tags_pushes_and_returns_registry_digest(monkeypatch):
    import subprocess

    from agent import executor

    calls: list[list[str]] = []
    mine = "reg:5000/lab/python-cpu@sha256:" + "ab" * 32
    hub = "python@sha256:" + "cd" * 32

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        out = '["%s","%s"]' % (hub, mine) if "{{json .RepoDigests}}" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    result = executor.mirror_image(source_image="python:3.12.7-slim", image_tag="reg:5000/lab/python-cpu:3.12.7-slim")
    assert result["ok"] and result["image_digest"] == mine
    assert [c[1] for c in calls[:3]] == ["pull", "tag", "push"]
    assert calls[1] == ["docker", "tag", "python:3.12.7-slim", "reg:5000/lab/python-cpu:3.12.7-slim"]


def test_mirror_image_reports_a_failed_push(monkeypatch):
    import subprocess

    from agent import executor

    def fake_run(cmd, **kwargs):
        failed = cmd[1] == "push"
        return subprocess.CompletedProcess(cmd, 1 if failed else 0, stdout="", stderr="denied" if failed else "")

    monkeypatch.setattr(executor.subprocess, "run", fake_run)
    result = executor.mirror_image(source_image="python:3.12", image_tag="reg:5000/lab/p:3.12")
    assert not result["ok"] and result["error"] == "denied"
