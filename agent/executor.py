"""Job executors: local Python or Docker. Docker is the v1 path when available."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

CPU_IMAGE = "python:3.12.7-slim"
LINE_CB = Callable[[str], None]


def _pump(proc: subprocess.Popen, on_line: LINE_CB, stop, deadline: float) -> str:
    """Forward output until the process exits, `stop` is set, or the deadline
    passes. Returns "exited", "stop" or "timeout".

    Output is read on a thread: a job that trains silently must still be
    cancellable and time-limited, which a blocking readline() would prevent.
    """
    lines: queue.Queue[str | None] = queue.Queue()

    def reader() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put(None)

    threading.Thread(target=reader, daemon=True).start()
    while True:
        if stop.is_set():
            return "stop"
        if time.time() > deadline:
            return "timeout"
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        if line is None:
            proc.wait()
            return "exited"
        on_line(line)
_REGISTRY_DIGEST = re.compile(r"^[^@\s]+/[^@\s]+@sha256:[0-9a-f]{64}$", re.IGNORECASE)


def is_registry_digest(value: str | None) -> bool:
    if not value:
        return False
    return _REGISTRY_DIGEST.match(value.strip()) is not None


def format_docker_error(err: str) -> str:
    text = (err or "docker info failed").strip()[:500]
    lowered = text.lower()
    if "permission denied" in lowered:
        return (
            text
            + "; add this user to the docker group (sudo usermod -aG docker $USER), "
            + "then from Windows run wsl --shutdown, or start with: "
            + "sg docker -c 'python3 agent.py --executor docker'"
        )
    return text


def docker_probe() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except FileNotFoundError:
        return False, "docker binary not on PATH (which docker is empty)"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        return True, ""
    err = (result.stderr or result.stdout or "docker info failed").strip()
    return False, format_docker_error(err)


def docker_available() -> bool:
    ok, _ = docker_probe()
    return ok


def nvidia_smi_ok() -> bool:
    try:
        result = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def resolve_image(image_digest: str | None) -> str:
    digest = (image_digest or "").strip()
    override = os.environ.get("LAB_IMAGE")
    if override:
        return override
    if not digest or digest.startswith("sha256:dev"):
        return CPU_IMAGE
    return digest


def container_name(attempt_id: str) -> str:
    return "lab-" + attempt_id.replace("_", "-")


def _world_readable(path: Path) -> None:
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    for child in path.rglob("*"):
        try:
            os.chmod(child, 0o755 if child.is_dir() else 0o644)
        except OSError:
            continue


def leftover_lab_ids(*, exclude: str | None = None) -> list[str]:
    try:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=lab-"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not exclude:
        return ids
    keep = container_name(exclude)
    named = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"name={keep}"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    keep_ids = set(named.stdout.split())
    return [i for i in ids if i not in keep_ids]


def reconcile_lab_containers(*, exclude: str | None = None) -> int:
    ids = leftover_lab_ids(exclude=exclude)
    removed = 0
    for container_id in ids:
        subprocess.run(
            ["docker", "rm", "-f", container_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
        removed += 1
    return removed


def build_docker_run_cmd(
    *,
    attempt_id: str,
    image: str,
    input_dir: Path,
    output_dir: Path,
    cache_dir: Path,
    tmp_dir: Path,
    entrypoint: str,
    args: list[str],
    cpu_cores: float,
    memory_mb: int,
    want_gpu: bool,
) -> list[str]:
    name = container_name(attempt_id)
    cmd = [
        "docker",
        "run",
        "--name",
        name,
        "--read-only",
        "--network",
        "none",
        "--pids-limit",
        "512",
        "--cpus",
        str(max(0.1, float(cpu_cores))),
        "--memory",
        f"{max(128, int(memory_mb))}m",
        "--tmpfs",
        "/tmp:size=2g",
        "-e",
        "OUTPUT_DIR=/output",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-v",
        f"{input_dir}:/input:ro",
        "-v",
        f"{output_dir}:/output",
        "-v",
        f"{cache_dir}:/cache",
        "-v",
        f"{tmp_dir}:/jobtmp",
        "--workdir",
        "/input",
    ]
    if want_gpu:
        # WSL + CDI: `device=0` and uid 10000 often hide the GPU even when
        # `docker run --gpus all` as root works. Keep the CPU isolation flags.
        cmd.extend(
            [
                "--gpus",
                "all",
                "-e",
                "NVIDIA_VISIBLE_DEVICES=all",
                "-e",
                "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
            ]
        )
    else:
        cmd.extend(["--user", "10000:10000"])
    entry = entrypoint if entrypoint.startswith("/") else "/input/" + entrypoint.lstrip("./")
    cmd.append(image)
    cmd.extend(["python", entry, *args])
    if any(part == "/var/run/docker.sock" or "docker.sock" in part for part in cmd):
        raise RuntimeError("docker socket must not be mounted into jobs")
    return cmd


def _ensure_image(image: str) -> str | None:
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if inspect.returncode == 0:
        return None
    pull = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if pull.returncode != 0:
        return pull.stderr.strip() or pull.stdout.strip() or "docker pull failed"
    return None


def run_docker(
    *,
    attempt_id: str,
    image_digest: str | None,
    input_dir: Path,
    output_dir: Path,
    entrypoint: str,
    args: list[str],
    resources: dict,
    timeout_seconds: int,
    stop,
    on_line: LINE_CB,
) -> tuple[int, str | None]:
    image = resolve_image(image_digest)
    pull_err = _ensure_image(image)
    if pull_err:
        on_line("environment: " + pull_err)
        return 1, "environment"

    cache_dir = output_dir.parent / "cache"
    tmp_dir = output_dir.parent / "tmp"
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    for path in (output_dir, cache_dir, tmp_dir):
        os.chmod(path, 0o777)
    _world_readable(input_dir)

    want_gpu = bool(int(resources.get("min_vram_mb") or 0) > 0) and nvidia_smi_ok()
    if int(resources.get("min_vram_mb") or 0) > 0 and not nvidia_smi_ok():
        on_line("executor: nvidia-smi missing; running without --gpus")
    on_line("executor docker image=%s gpus=%s" % (image, "yes" if want_gpu else "no"))
    cmd = build_docker_run_cmd(
        attempt_id=attempt_id,
        image=image,
        input_dir=input_dir,
        output_dir=output_dir,
        cache_dir=cache_dir,
        tmp_dir=tmp_dir,
        entrypoint=entrypoint,
        args=args,
        cpu_cores=float(resources.get("cpu_cores") or 1),
        memory_mb=int(resources.get("ram_mb") or 512),
        want_gpu=want_gpu,
    )
    name = container_name(attempt_id)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.time() + max(1, int(timeout_seconds))
    category = None
    try:
        outcome = _pump(proc, on_line, stop, deadline)
        if outcome == "stop":
            subprocess.run(["docker", "kill", "--signal", "SIGTERM", name], capture_output=True, timeout=10, check=False)
            time.sleep(5)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
            proc.kill()
            return 143, "cancelled"
        if outcome == "timeout":
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=10, check=False)
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
            proc.kill()
            return 124, "timeout"
        code = proc.wait()
        if code == 137:
            category = "resource"
        elif code != 0:
            category = "user_code"
        return code, category
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)


def run_local_python(
    *,
    python_bin: str,
    input_dir: Path,
    output_dir: Path,
    entrypoint: str,
    args: list[str],
    timeout_seconds: int,
    stop,
    on_line: LINE_CB,
) -> tuple[int, str | None]:
    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(output_dir)
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [python_bin, entrypoint, *args],
        cwd=str(input_dir),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert proc.stdout is not None
    deadline = time.time() + max(1, int(timeout_seconds))
    outcome = _pump(proc, on_line, stop, deadline)
    if outcome == "stop":
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        return proc.returncode if proc.returncode is not None else 143, "cancelled"
    if outcome == "timeout":
        proc.kill()
        proc.wait()
        return 124, "timeout"
    code = proc.wait()
    if code == 0:
        return 0, None
    return code, "user_code"


def _req_import_name(line: str) -> str:
    pkg = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].split("!=")[0]
    pkg = pkg.split("[")[0].strip().replace("-", "_")
    aliases = {"scikit_learn": "sklearn", "pillow": "PIL", "opencv_python": "cv2", "pyyaml": "yaml"}
    return aliases.get(pkg, pkg)


def build_custom_image(
    *,
    base_image: str,
    image_tag: str,
    requirements: str,
    need_cuda: bool,
    work_dir: Path,
    timeout_seconds: int = 1200,
) -> dict:
    log: list[str] = []

    def note(line: str) -> None:
        log.append(line)

    # Same mapping as train runs: sha256:dev-* → python slim, not a Hub repo named "sha256".
    base = resolve_image(base_image)
    note("base_image %s -> %s" % (base_image, base))
    pull_err = _ensure_image(base)
    if pull_err:
        return {"ok": False, "image_digest": None, "lockfile": "", "log": "\n".join(log + [pull_err]), "error": pull_err}

    ctx = work_dir / "envbuild"
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "requirements.txt").write_text(requirements if requirements.endswith("\n") else requirements + "\n")
    (ctx / "Dockerfile").write_text(
        "FROM %s\nCOPY requirements.txt /tmp/requirements.txt\n"
        "RUN pip install --no-cache-dir -r /tmp/requirements.txt\n" % base
    )
    build = subprocess.run(
        ["docker", "build", "-t", image_tag, str(ctx)],
        capture_output=True,
        text=True,
        timeout=max(60, int(timeout_seconds)),
        check=False,
    )
    note(build.stdout or "")
    note(build.stderr or "")
    if build.returncode != 0:
        err = (build.stderr or build.stdout or "docker build failed").strip()[:500]
        return {"ok": False, "image_digest": None, "lockfile": "", "log": "\n".join(log), "error": err}

    cmd = ["docker", "run", "--rm"]
    if need_cuda and nvidia_smi_ok():
        cmd.extend(["--gpus", "all"])
    torch_base = "pytorch" in base.lower()
    test_src = "import subprocess, sys\n"
    if need_cuda or torch_base:
        test_src += (
            "import torch\n"
            "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())\n"
        )
        if need_cuda:
            test_src += "assert torch.cuda.is_available(), 'cuda missing'\n"
    test_src += "print(subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True))\n"
    names = [_req_import_name(line) for line in requirements.splitlines() if line.strip() and not line.strip().startswith("#")]
    for name in names:
        if name:
            test_src += "import importlib; importlib.import_module(%r)\n" % name
    test = subprocess.run(
        cmd + [image_tag, "python", "-c", test_src],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    note(test.stdout or "")
    note(test.stderr or "")
    if test.returncode != 0:
        err = (test.stderr or test.stdout or "import/cuda test failed").strip()[:500]
        return {"ok": False, "image_digest": None, "lockfile": "", "log": "\n".join(log), "error": err}
    freeze_lines = [ln for ln in (test.stdout or "").splitlines() if "==" in ln]
    lockfile = "\n".join(freeze_lines) + ("\n" if freeze_lines else "")
    if "/" not in image_tag or image_tag.startswith("sha256:"):
        err = "image tag is not a registry repository; set REGISTRY_URL"
        return {"ok": False, "image_digest": None, "lockfile": lockfile, "log": "\n".join(log + [err]), "error": err}
    note("pushing " + image_tag)
    push = subprocess.run(
        ["docker", "push", image_tag],
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    note(push.stdout or "")
    note(push.stderr or "")
    if push.returncode != 0:
        err = (push.stderr or push.stdout or "docker push failed").strip()[:500]
        return {"ok": False, "image_digest": None, "lockfile": lockfile, "log": "\n".join(log), "error": err}
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{index .RepoDigests 0}}", image_tag],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    digest = (inspect.stdout or "").strip()
    if not is_registry_digest(digest):
        err = "push succeeded but RepoDigests is not a registry digest: " + digest
        return {"ok": False, "image_digest": None, "lockfile": lockfile, "log": "\n".join(log + [err]), "error": err}
    return {"ok": True, "image_digest": digest, "lockfile": lockfile or requirements, "log": "\n".join(log), "error": None}


def pick_mode(requested: str) -> str:
    requested = (requested or "auto").lower()
    if requested == "python":
        return "python"
    if requested == "docker":
        ok, detail = docker_probe()
        if not ok:
            raise SystemExit("docker executor requested but docker is not available: " + detail)
        return "docker"
    return "docker" if docker_available() else "python"
