"""Job executors: local Python or Docker. Docker is the v1 path when available."""
from __future__ import annotations

import json
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


STAGE_CB = Callable[[str, str, "dict | None"], None]
_LAYER_LINE = re.compile(r"^([0-9a-f]{6,64}): (.+?)\s*$", re.IGNORECASE)


class PullProgress:
    """Layer progress of a `docker pull`, read from its output lines.

    A layer is counted once its line first appears ("Pulling fs layer",
    "Already exists", ...) and done at "Pull complete" or "Already exists"."""

    def __init__(self) -> None:
        self.seen: set[str] = set()
        self.done: set[str] = set()

    def feed(self, line: str) -> dict | None:
        """The progress dict if this line changed it, else None."""
        match = _LAYER_LINE.match(line.strip())
        if not match:
            return None
        layer, status = match.group(1).lower(), match.group(2).lower()
        if status.startswith("pulling from"):  # "<tag>: Pulling from <repo>", not a layer
            return None
        before = (len(self.seen), len(self.done))
        self.seen.add(layer)
        if status.endswith("pull complete") or status.endswith("already exists"):
            self.done.add(layer)
        if before == (len(self.seen), len(self.done)):
            return None
        return self.progress()

    def progress(self) -> dict:
        return {"done": len(self.done), "total": len(self.seen), "unit": "layers"}


def _ensure_image(image: str, *, stop=None, on_stage: STAGE_CB | None = None) -> str | None:
    """Make `image` available locally. Returns an error message, or None.

    A cached image is not a stage. A real pull is reported as `pulling_image`
    with layer progress; it can be cancelled through `stop`."""
    inspect = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    if inspect.returncode == 0:
        return None
    report = on_stage or (lambda _name, _state, _progress: None)
    tail: list[str] = []
    tracker = PullProgress()

    def on_pull_line(line: str) -> None:
        tail.append(line)
        del tail[:-20]
        progress = tracker.feed(line)
        if progress is not None:
            report("pulling_image", "started", progress)

    report("pulling_image", "started", None)
    proc = subprocess.Popen(
        ["docker", "pull", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        outcome = _pump(proc, on_pull_line, stop or threading.Event(), time.time() + 300)
        if outcome != "exited":
            proc.kill()
            return "docker pull cancelled" if outcome == "stop" else "docker pull timed out"
        if proc.returncode != 0:
            return "\n".join(tail).strip() or "docker pull failed"
        return None
    finally:
        report("pulling_image", "finished", tracker.progress() if tracker.seen else None)


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
    on_stage: STAGE_CB | None = None,
) -> tuple[int, str | None]:
    image = resolve_image(image_digest)
    pull_err = _ensure_image(image, stop=stop, on_stage=on_stage)
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
    if on_stage:
        on_stage("running", "started", None)
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
    on_stage: STAGE_CB | None = None,
) -> tuple[int, str | None]:
    env = os.environ.copy()
    env["OUTPUT_DIR"] = str(output_dir)
    env["PYTHONUNBUFFERED"] = "1"
    if on_stage:
        on_stage("running", "started", None)
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


def should_check_cuda(
    *, need_cuda: bool, build_class: str | None, builder_class: str | None, has_gpu: bool
) -> bool:
    """A build is only CUDA-tested on a builder that has the same kind of GPU.

    Any builder can build for any class; a builder without that GPU still runs
    the import test, which is what it can vouch for.
    """
    if not need_cuda or not has_gpu:
        return False
    return bool(build_class) and (build_class or "").strip() == (builder_class or "").strip()


def build_custom_image(
    *,
    base_image: str,
    image_tag: str,
    requirements: str,
    need_cuda: bool,
    work_dir: Path,
    timeout_seconds: int = 1200,
    build_class: str | None = None,
    builder_class: str | None = None,
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
    check_cuda = should_check_cuda(
        need_cuda=need_cuda,
        build_class=build_class,
        builder_class=builder_class,
        has_gpu=nvidia_smi_ok(),
    )
    if need_cuda and not check_cuda:
        note("cuda check skipped: builder has no %s GPU" % (build_class or "matching"))
    if check_cuda:
        cmd.extend(["--gpus", "all"])
    torch_base = "pytorch" in base.lower()
    test_src = "import subprocess, sys\n"
    if need_cuda or torch_base:
        test_src += (
            "import torch\n"
            "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())\n"
        )
        if check_cuda:
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


def split_ref(ref: str) -> tuple[str, str]:
    """"host:5000/a/b:tag" -> ("host:5000/a/b", ":tag"); "a@sha256:x" -> ("a", "@sha256:x")."""
    ref = ref.strip()
    if "@" in ref:
        repo, digest = ref.split("@", 1)
        return repo, "@" + digest
    head, _, last = ref.rpartition("/")
    name, colon, tag = last.partition(":")
    repo = (head + "/" if head else "") + name
    return repo, (colon + tag) if colon else ""


def pick_repo_digest(digests: list[str], image_tag: str) -> str | None:
    """The RepoDigest for image_tag's repository. A pulled-then-pushed image also
    lists the digest it was pulled from, so index 0 is not always the right one."""
    repo = split_ref(image_tag)[0]
    for digest in digests:
        if digest.startswith(repo + "@") and is_registry_digest(digest):
            return digest
    return None


def mirror_image(
    *, source_image: str, image_tag: str, timeout_seconds: int = 3600
) -> dict:
    """Copy source_image into the lab registry as image_tag; no pip, no tests."""
    log: list[str] = []

    def fail(err: str) -> dict:
        return {"ok": False, "image_digest": None, "lockfile": "", "log": "\n".join(log + [err]), "error": err}

    if "/" not in image_tag or image_tag.startswith("sha256:"):
        return fail("image tag is not a registry repository; set REGISTRY_URL")
    steps = [
        ("pulling " + source_image, ["docker", "pull", source_image]),
        ("tagging " + image_tag, ["docker", "tag", source_image, image_tag]),
        ("pushing " + image_tag, ["docker", "push", image_tag]),
    ]
    for label, cmd in steps:
        log.append(label)
        try:
            run = subprocess.run(
                cmd, capture_output=True, text=True, timeout=max(60, int(timeout_seconds)), check=False
            )
        except subprocess.TimeoutExpired:
            return fail("%s timed out" % cmd[1])
        log.extend([run.stdout or "", run.stderr or ""])
        if run.returncode != 0:
            return fail(((run.stderr or run.stdout or "docker %s failed" % cmd[1]).strip())[:500])
    inspect = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_tag],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    try:
        digests = [str(d) for d in json.loads(inspect.stdout or "[]") or []]
    except ValueError:
        digests = []
    digest = pick_repo_digest(digests, image_tag)
    if digest is None:
        return fail("push succeeded but RepoDigests has no digest for %s: %s" % (image_tag, digests))
    return {"ok": True, "image_digest": digest, "lockfile": "", "log": "\n".join(log), "error": None}


# --- Image cache pruning -------------------------------------------------------

LAB_REPO_MARKERS = ("/lab-env/", "/lab/")


def normalize_ref(ref: str) -> str:
    repo, suffix = split_ref(ref)
    return repo + (suffix or ":latest")


def is_lab_repo(repository: str) -> bool:
    return any(marker in repository + "/" for marker in LAB_REPO_MARKERS)


def _short_id(image_id: str) -> str:
    return image_id.removeprefix("sha256:")[:12]


def select_images_to_prune(
    images: list[dict],
    last_used: dict[str, float],
    running_ids: set[str],
) -> list[dict]:
    """Images the agent may remove, least recently used first.

    images: [{"id": "sha256:...", "refs": ["repo:tag" | "repo@sha256:..."]}], one per
    image id. Only lab images qualify: a repository under /lab-env/ or /lab/, or a
    reference the agent itself ran or built (catalog images). Anything a container
    (running or stopped) is using stays.
    """
    used = {normalize_ref(ref): ts for ref, ts in last_used.items()}
    running = {_short_id(i) for i in running_ids}
    picked: list[tuple[float, str, dict]] = []
    for image in images:
        if _short_id(image["id"]) in running:
            continue
        refs = [normalize_ref(r) for r in image.get("refs") or []]
        if not refs:
            continue
        # Only our own references are ever removed; a tag the machine's owner put
        # on the same image stays, and so does the image.
        ours = [r for r in refs if is_lab_repo(split_ref(r)[0]) or r in used]
        if not ours:
            continue
        stamp = max((used.get(r, 0.0) for r in refs), default=0.0)
        picked.append((stamp, image["id"], {**image, "refs": ours}))
    picked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in picked]


def list_local_images() -> list[dict]:
    """One entry per image id with every tag/digest reference it carries."""
    try:
        result = subprocess.run(
            ["docker", "image", "ls", "--no-trunc", "--digests", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    by_id: dict[str, dict] = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        repo, tag, digest = row.get("Repository"), row.get("Tag"), row.get("Digest")
        if not repo or repo == "<none>":
            continue
        if tag and tag != "<none>":
            ref = "%s:%s" % (repo, tag)
        elif digest and digest != "<none>":
            ref = "%s@%s" % (repo, digest)
        else:
            continue
        entry = by_id.setdefault(row.get("ID", ""), {"id": row.get("ID", ""), "refs": []})
        entry["refs"].append(ref)
    return [e for e in by_id.values() if e["id"]]


def running_image_ids() -> set[str]:
    """Image ids of every container, running or stopped."""
    try:
        ps = subprocess.run(
            ["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True, timeout=15, check=False
        )
        ids = ps.stdout.split()
        if not ids:
            return set()
        inspect = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", *ids],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return set(inspect.stdout.split())


def prune_images(
    *,
    last_used: dict[str, float],
    free_bytes: Callable[[], int],
    watermark: int,
    log: LINE_CB = print,
) -> list[str]:
    """Remove lab images, least recently used first, until free space is back above
    3x the watermark. Starts only when free space is under 2x the watermark.
    Returns the references removed."""
    if watermark <= 0 or free_bytes() >= 2 * watermark:
        return []
    removed: list[str] = []
    for image in select_images_to_prune(list_local_images(), last_used, running_image_ids()):
        if free_bytes() >= 3 * watermark:
            break
        for ref in image["refs"]:
            rm = subprocess.run(
                ["docker", "image", "rm", ref], capture_output=True, text=True, timeout=120, check=False
            )
            if rm.returncode == 0:
                removed.append(ref)
                log("image cache: removed %s (free %d MB)" % (ref, free_bytes() // (1024 * 1024)))
            else:
                log("image cache: could not remove %s: %s" % (ref, (rm.stderr or rm.stdout).strip()[:200]))
                break
    return removed


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
