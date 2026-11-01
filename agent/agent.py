#!/usr/bin/env python3
"""Pull-based lab agent. Stdlib only. Run on any desktop on the same Wi-Fi."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    from classify import agent_register_fields, collect_health, collect_usage, disk_free_bytes, gpu_desktop_busy
    from executor import (
        build_custom_image,
        container_name,
        is_registry_digest,
        mirror_image,
        normalize_ref,
        pick_mode,
        prune_images,
        reconcile_lab_containers,
        resolve_image,
        run_docker,
        run_local_python,
    )
    import updater
except ImportError:
    from agent.classify import (
        agent_register_fields,
        collect_health,
        collect_usage,
        disk_free_bytes,
        gpu_desktop_busy,
    )
    from agent.executor import (
        build_custom_image,
        container_name,
        is_registry_digest,
        mirror_image,
        normalize_ref,
        pick_mode,
        prune_images,
        reconcile_lab_containers,
        resolve_image,
        run_docker,
        run_local_python,
    )
    from agent import updater

AGENT_VERSION = "0.7.1"

BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
CRASH_TRACE_LINES = 40
LOG_FILE_NAME = "agent.log"
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_UPLOAD_LINES = 500


class LogFile:
    """Append-only log file that rotates at `max_bytes`; one old file is kept as
    `<name>.1`. A failing disk never breaks the agent."""

    def __init__(self, path: Path, max_bytes: int = LOG_MAX_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        self.size = 0
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.size = path.stat().st_size if path.exists() else 0
        except OSError:
            pass

    def append(self, chunk: str) -> None:
        with self.lock:
            try:
                if self.size + len(chunk) > self.max_bytes:
                    os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                    self.size = 0
                with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                    fh.write(chunk)
                self.size += len(chunk)
            except OSError:
                pass


class LogTee:
    """Stand-in for sys.stdout / sys.stderr: passes everything through and also
    writes each finished line, with a timestamp, to the log file."""

    def __init__(self, stream, log: LogFile) -> None:
        self.stream = stream
        self.log = log
        self.partial = ""

    def write(self, text: str) -> int:
        try:
            self.stream.write(text)
        except (OSError, ValueError):
            pass
        *lines, self.partial = (self.partial + text).split("\n")
        if lines:
            stamp = _iso(time.time())
            self.log.append("".join("%s %s\n" % (stamp, line) for line in lines))
        return len(text)

    def flush(self) -> None:
        try:
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        return getattr(self.stream, name)


def install_log_tee(path: Path) -> None:
    """Mirror stdout and stderr into `path` (once per process)."""
    log = LogFile(path)
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, LogTee):
            setattr(sys, name, LogTee(stream, log))


def read_log_tail(path: Path, lines: int = LOG_UPLOAD_LINES) -> str:
    """The last `lines` lines of the agent log, reaching back into the rotated
    file when the current one is short."""
    collected: list[str] = []
    for candidate in (path, path.with_name(path.name + ".1")):
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        collected = text.splitlines() + collected
        if len(collected) >= lines:
            break
    tail = collected[-lines:]
    return "\n".join(tail) + "\n" if tail else ""


def read_boot_id(path: Path = BOOT_ID_PATH) -> str | None:
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def read_last_exit(path: Path) -> tuple[int, float] | None:
    """The launcher's "<exit-code> <unix-ts>" record of how the last run ended."""
    try:
        code, stamp = path.read_text().split()[:2]
        return int(code), float(stamp)
    except (OSError, ValueError):
        return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def decide_restart(
    *,
    previously_registered: bool,
    boot_id: str | None,
    prev_boot_id: str | None,
    last_register_at: float | None,
    last_exit: tuple[int, float] | None,
    crash: tuple[float, str] | None,
    now: float,
) -> dict:
    """Why this agent process started. Pure: every input is read by the caller.

    last_exit is (exit code, unix time) from the launcher; crash is (mtime, text)
    of last_crash.txt. Both only count when they are newer than the last register,
    i.e. they describe the run that just ended.
    """

    def newer(ts: float) -> bool:
        return last_register_at is None or ts > last_register_at

    exit_code = last_exit[0] if last_exit and newer(last_exit[1]) else None
    if not previously_registered:
        return {"reason": "first_start", "exit_code": None, "detail": None, "at": None}
    if boot_id and prev_boot_id and boot_id != prev_boot_id:
        return {"reason": "reboot", "exit_code": None, "detail": None, "at": _iso(now)}
    if exit_code == updater.EXIT_UPDATED:
        return {"reason": "update", "exit_code": exit_code, "detail": None, "at": _iso(last_exit[1])}
    if crash is not None and newer(crash[0]):
        return {"reason": "crash", "exit_code": exit_code, "detail": crash[1][-4000:], "at": _iso(crash[0])}
    at = _iso(last_exit[1]) if exit_code is not None else None
    return {"reason": "manual", "exit_code": exit_code, "detail": None, "at": at}


def load_config(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise SystemExit("config must be a JSON object")
    return data


def _dir_is_writable(path: Path) -> bool:
    try:
        if path.exists():
            if not path.is_dir():
                return False
            if not os.access(path, os.W_OK | os.X_OK):
                return False
            probe = path / (".lab-write-%s" % os.getpid())
            probe.write_text("ok")
            probe.unlink()
            return True
        path.mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK | os.X_OK)
    except OSError:
        return False


def resolve_attempt_root(
    explicit: Path | None = None,
    *,
    system_root: Path | None = None,
) -> Path:
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env = os.environ.get("LAB_ATTEMPT_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.append(system_root or Path("/var/lab/attempts"))
    candidates.append(Path.home() / ".distributetr9" / "attempts")
    seen: set[Path] = set()
    for cand in candidates:
        resolved = cand.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _dir_is_writable(resolved):
            return resolved
    raise PermissionError(
        "no writable attempt directory (tried /var/lab/attempts and ~/.distributetr9/attempts); "
        "fix with: sudo chown $USER /var/lab/attempts"
    )


def attempt_dir(attempt_id: str, root: Path | None) -> Path:
    base = resolve_attempt_root(root)
    work = base / attempt_id
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    return work


HEALTH_REFRESH_SECONDS = 60
USAGE_SAMPLE_SECONDS = 5
MAX_PENDING_STAGES = 20


class Telemetry:
    """Samples tools and queues stage events on behalf of the heartbeat and the
    lease-renew loop.

    nvidia-smi and docker run on their own thread, so a slow or hung tool can
    delay a sample but never a heartbeat or a lease renewal; those only read
    what was last collected."""

    def __init__(self, *, check_docker: bool) -> None:
        self.check_docker = check_docker
        self.lock = threading.Lock()
        self.health: dict | None = None
        self.container: str | None = None
        self.sampling = False
        self.usage: dict | None = None
        self.stages: list[dict] = []
        # Set when a stage starts or ends so the renew loop reports it at once.
        self.wake = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def _loop(self) -> None:
        next_health = 0.0
        while True:
            try:
                if time.time() >= next_health:
                    health = collect_health(check_docker=self.check_docker)
                    with self.lock:
                        self.health = health or None
                    next_health = time.time() + HEALTH_REFRESH_SECONDS
                with self.lock:
                    sampling, container = self.sampling, self.container
                if sampling:
                    usage = collect_usage(container)
                    with self.lock:
                        if self.sampling:
                            self.usage = usage
            except Exception:
                pass
            time.sleep(USAGE_SAMPLE_SECONDS)

    def health_snapshot(self) -> dict | None:
        with self.lock:
            return dict(self.health) if self.health else None

    def begin_job(self, container: str | None) -> None:
        with self.lock:
            self.container, self.sampling = container, True
            self.usage, self.stages = None, []
        self.wake.clear()
        self.start()

    def end_job(self) -> None:
        with self.lock:
            self.sampling, self.container, self.usage = False, None, None

    def push_stage(self, name: str, state: str, progress: dict | None = None) -> None:
        with self.lock:
            last = self.stages[-1] if self.stages else None
            if last and last["name"] == name and last["state"] == "started" and state == "started":
                # A progress update replaces the previous one instead of queueing.
                last["progress"] = progress or last["progress"]
            else:
                self.stages.append({"name": name, "state": state, "progress": progress})
        if state == "finished" or progress is None:
            self.wake.set()

    def take(self) -> tuple[dict | None, list[dict]]:
        """The newest usage sample and the queued stage events, now owned by the caller."""
        with self.lock:
            usage, stages = self.usage, self.stages
            self.usage, self.stages = None, []
        return usage, stages

    def restore(self, stages: list[dict]) -> None:
        """Put events back after a renew that did not get through."""
        with self.lock:
            self.stages = (stages + self.stages)[-MAX_PENDING_STAGES:]


def http_json(method: str, url: str, body=None, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            payload = json.loads(raw.decode() or "null")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            payload = {"error": "http", "message": raw.decode(errors="replace")}
        return exc.code, payload


def http_bytes(method: str, url: str, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Agent:
    def __init__(
        self,
        api: str,
        state_path: Path,
        python_bin: str,
        executor: str = "auto",
        *,
        cuda_test: bool = False,
        attempt_root: Path | None = None,
        class_override: str | None = None,
        role: str = "train",
        bundle_public_key: dict | None = None,
        room: str | None = None,
    ) -> None:
        self.api = api.rstrip("/")
        self.state_path = state_path
        self.python_bin = python_bin
        self.executor = pick_mode(executor)
        self.cuda_test = cuda_test
        self.attempt_root = attempt_root
        self.class_override = class_override
        self.role = "build" if (role or "train").strip().lower() == "build" else "train"
        # Where this PC sits, from the install config; the server keeps an admin's choice over it.
        self.room = str(room or "").strip() or None
        self.healthy = True
        self.busy = False
        self.state = {}
        if state_path.exists():
            self.state = json.loads(state_path.read_text())
        # A key from the install config wins; otherwise the first key the
        # server sends is pinned and never replaced (trust on first use).
        if bundle_public_key:
            self.state["bundle_public_key"] = bundle_public_key
        self.release_root = updater.release_root()
        self.update_backoff: dict[str, float] = {}
        self.compat_class: str | None = None
        self.disk_watermark = 0
        self.next_prune_at = 0.0
        self.restart_reported = False
        self.telemetry = Telemetry(check_docker=self.executor == "docker")
        self.log_upload_lock = threading.Lock()

    @property
    def crash_path(self) -> Path:
        return self.state_path.parent / "last_crash.txt"

    @property
    def log_path(self) -> Path:
        return self.state_path.parent / LOG_FILE_NAME

    @property
    def last_exit_path(self) -> Path:
        root = self.release_root or Path(os.environ.get("LAB_AGENT_ROOT") or "/opt/lab-agent")
        return root / "last_exit"

    def restart_info(self) -> dict | None:
        """Why this process started; reported once per process, on its first register."""
        if self.restart_reported:
            return None
        crash = None
        try:
            crash = (self.crash_path.stat().st_mtime, self.crash_path.read_text())
        except OSError:
            pass
        return decide_restart(
            previously_registered=bool(self.state.get("node_id")),
            boot_id=read_boot_id(),
            prev_boot_id=self.state.get("boot_id"),
            last_register_at=self.state.get("last_register_at"),
            last_exit=read_last_exit(self.last_exit_path),
            crash=crash,
            now=time.time(),
        )

    def note_image_use(self, *refs: str) -> None:
        """Remember when each image was last used, for LRU pruning."""
        used = self.state.setdefault("image_last_used", {})
        now = time.time()
        for ref in refs:
            if ref:
                used[normalize_ref(ref)] = now
        self.save()

    def maybe_prune_images(self) -> None:
        if self.executor != "docker" or self.disk_watermark <= 0 or time.time() < self.next_prune_at:
            return

        def free() -> int:
            return disk_free_bytes("/", str(Path.home()))

        if free() >= 2 * self.disk_watermark:
            return
        used = self.state.get("image_last_used") or {}
        removed = prune_images(
            last_used=used,
            free_bytes=free,
            watermark=self.disk_watermark,
            log=lambda line: print(line, flush=True),
        )
        for ref in removed:
            used.pop(normalize_ref(ref), None)
        if removed:
            self.save()
        # Nothing left to remove: do not run docker image ls on every loop.
        self.next_prune_at = 0.0 if removed else time.time() + 300

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, indent=2))

    def headers(self) -> dict:
        hdrs = {
            "Authorization": "Bearer " + self.state["node_token"],
            "X-Node-Id": self.state["node_id"],
            "X-Boot-Session-Id": self.state["boot_session_id"],
        }
        return hdrs

    def register(self) -> None:
        caps = agent_register_fields(python_bin=self.python_bin, cuda_test=self.cuda_test)
        if self.class_override:
            caps["compatibility_class"] = self.class_override
        self.healthy = bool(caps["agent_healthy"])
        self.busy = bool(caps["gpu_desktop_busy"])
        self.compat_class = caps["compatibility_class"]
        last_restart = self.restart_info()
        body = {
            "node_id": self.state.get("node_id"),
            "hostname": socket.gethostname(),
            "agent_version": AGENT_VERSION,
            "gpu_model": caps["gpu_model"],
            "vram_total_mb": caps["vram_total_mb"],
            "vram_allocatable_mb": caps["vram_allocatable_mb"],
            "driver_version": caps["driver_version"],
            "compute_capability": caps["compute_capability"],
            "compatibility_class": caps["compatibility_class"],
            "cuda_preflight_ok": caps["cuda_preflight_ok"],
            "cpu_cores": caps["cpu_cores"],
            "ram_mb": caps["ram_mb"],
            "disk_free_bytes": caps["disk_free_bytes"],
            "windows_version": caps["windows_version"],
            "wsl_version": caps["wsl_version"],
            "role": self.role,
            "last_restart": last_restart,
        }
        if self.room:
            body["room"] = self.room
        headers = {}
        if self.state.get("node_token") and self.state.get("node_id"):
            headers["Authorization"] = "Bearer " + self.state["node_token"]
        else:
            body["node_id"] = None
        status, payload = http_json("POST", self.api + "/agent/register", body, headers)
        if status >= 400:
            raise SystemExit("register failed: %s %s" % (status, payload))
        self.state["node_id"] = payload["node_id"]
        self.state["node_token"] = payload["node_token"]
        self.state["boot_session_id"] = payload["boot_session_id"]
        self.state["lease_ttl_seconds"] = payload.get("lease_ttl_seconds", 30)
        if payload.get("bundle_public_key") and not self.state.get("bundle_public_key"):
            self.state["bundle_public_key"] = payload["bundle_public_key"]
        self.state["boot_id"] = read_boot_id()
        self.state["last_register_at"] = time.time()
        self.save()
        self.restart_reported = True
        try:
            self.crash_path.unlink()
        except OSError:
            pass
        if updater.confirm_running(self.release_root, AGENT_VERSION):
            print("release", AGENT_VERSION, "confirmed", flush=True)
            updater.prune_releases(self.release_root)
        print(
            "registered node",
            self.state["node_id"],
            "class",
            caps["compatibility_class"],
            "executor",
            self.executor,
            "role",
            self.role,
            "healthy",
            self.healthy,
            "attempt_root",
            resolve_attempt_root(self.attempt_root),
            flush=True,
        )
        if self.executor == "docker":
            removed = reconcile_lab_containers()
            if removed:
                print("reconciled leftover containers", removed, flush=True)

    def heartbeat(self, attempt_id=None):
        self.busy = gpu_desktop_busy(has_lab_job=attempt_id is not None)
        body = {
            "disk_linux_free_bytes": disk_free_bytes("/", str(Path.home())),
            "disk_windows_free_bytes": disk_free_bytes("C:\\") if os.name == "nt" else 0,
            "gpu_desktop_busy": self.busy,
            "current_attempt_id": attempt_id,
            "agent_healthy": self.healthy,
        }
        health = self.telemetry.health_snapshot()
        if health:
            body["health"] = health
        return http_json("POST", self.api + "/agent/heartbeat", body, self.headers())

    def handle_requests(self, hb) -> None:
        """Things the server asked for in a heartbeat response (see `requests`)."""
        requests = (hb or {}).get("requests") if isinstance(hb, dict) else None
        if not isinstance(requests, list) or "upload_logs" not in requests:
            return
        if not self.log_upload_lock.acquire(blocking=False):
            return  # one is already on its way

        def upload() -> None:
            try:
                self.upload_logs()
            finally:
                self.log_upload_lock.release()

        # A thread, so a slow upload never delays a lease renewal.
        threading.Thread(target=upload, daemon=True).start()

    def upload_logs(self) -> None:
        body = read_log_tail(self.log_path).encode("utf-8", errors="replace")
        headers = {**self.headers(), "Content-Type": "text/plain; charset=utf-8"}
        try:
            status, _ = http_bytes("POST", self.api + "/agent/logs-upload", data=body, headers=headers)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print("log upload failed", exc, flush=True)
            return
        print("log upload", status, len(body), "bytes", flush=True)

    def claim(self):
        return http_json(
            "POST",
            self.api + "/agent/claim",
            {},
            self.headers(),
            timeout=35,
        )

    def run_forever(self) -> None:
        self.register()
        self.telemetry.start()
        while True:
            try:
                status, hb = self.heartbeat()
                command = (hb or {}).get("command") if isinstance(hb, dict) else None
                if status >= 400:
                    print("heartbeat error", status, hb, flush=True)
                    time.sleep(3)
                    continue
                self.disk_watermark = int((hb or {}).get("disk_watermark_bytes") or 0) if isinstance(hb, dict) else 0
                self.handle_requests(hb)
                update = (hb or {}).get("agent_update") if isinstance(hb, dict) else None
                if update and self.maybe_update(update):
                    sys.exit(updater.EXIT_UPDATED)
                if not self.healthy:
                    print("unhealthy, not claiming", flush=True)
                    time.sleep(10)
                    continue
                if self.busy:
                    print("gpu desktop busy, not claiming", flush=True)
                    time.sleep(5)
                    continue
                if command in {"quarantine", "drain"}:
                    print("command", command, "not claiming", flush=True)
                    time.sleep(5)
                    continue
                self.maybe_prune_images()
                status, payload = self.claim()
                if status >= 400:
                    print("claim error", status, payload, flush=True)
                    if payload.get("error") == "stale_boot_session":
                        self.register()
                    time.sleep(2)
                    continue
                attempt = (payload or {}).get("attempt")
                build = (payload or {}).get("build")
                if attempt:
                    self.execute(attempt)
                elif build:
                    self.execute_build(build)
                else:
                    print("no work", flush=True)
            except PermissionError as exc:
                print("permission error", exc, flush=True)
                time.sleep(3)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print("network error", exc, flush=True)
                time.sleep(3)

    def run_logged(self) -> None:
        """run_forever, leaving the end of the traceback behind if it dies."""
        try:
            self.run_forever()
        except Exception:
            trace = traceback.format_exc().splitlines()[-CRASH_TRACE_LINES:]
            try:
                self.crash_path.parent.mkdir(parents=True, exist_ok=True)
                self.crash_path.write_text("\n".join(trace) + "\n")
            except OSError:
                pass
            raise

    def execute(self, attempt: dict) -> None:
        attempt_id = attempt["attempt_id"]
        token = attempt["fencing_token"]
        print("claimed", attempt_id, "job", attempt["job_id"], flush=True)
        fence = {"X-Fencing-Token": token, **self.headers()}
        stop = threading.Event()
        renew_err = []
        self.heartbeat(attempt_id)

        tel = self.telemetry
        tel.begin_job(container_name(attempt_id) if self.executor == "docker" else None)

        def renew_once() -> None:
            usage, stages = tel.take()
            body = {"fencing_token": token, "observed_state": "running"}
            if usage:
                body["usage"] = usage
            if stages:
                body["stages"] = stages
            try:
                st, resp = http_json("POST", "%s/attempts/%s/renew" % (self.api, attempt_id), body, self.headers())
            except (urllib.error.URLError, TimeoutError, OSError):
                tel.restore(stages)
                return
            if st >= 500:
                tel.restore(stages)
            if st == 409:
                renew_err.append(resp)
                stop.set()
            elif (resp or {}).get("desired_state") == "cancel":
                renew_err.append({"error": "cancel"})
                stop.set()

        def renew_loop():
            interval = max(5, int(self.state.get("lease_ttl_seconds", 30)) / 3)
            next_at = time.time() + interval
            # A stage change wakes the loop early; otherwise it renews every interval.
            while not stop.wait(0.5):
                due = time.time() >= next_at
                if not due and not tel.wake.is_set():
                    continue
                tel.wake.clear()
                if due:
                    next_at = time.time() + interval
                    try:
                        self.handle_requests(self.heartbeat(attempt_id)[1])
                    except (urllib.error.URLError, TimeoutError, OSError):
                        pass
                renew_once()

        t = threading.Thread(target=renew_loop, daemon=True)
        t.start()
        work = None
        try:
            work = attempt_dir(attempt_id, self.attempt_root)
            http_json(
                "POST",
                "%s/attempts/%s/acknowledge" % (self.api, attempt_id),
                {"fencing_token": token, "state": "starting"},
                self.headers(),
            )
            _st, archive = http_bytes(
                "GET",
                "%s/attempts/%s/input" % (self.api, attempt_id),
                headers=fence,
            )
            archive_path = work / "input.bin"
            archive_path.write_bytes(archive)
            src = work / "src"
            src.mkdir()
            extract_archive(archive_path, src)
            http_json(
                "POST",
                "%s/attempts/%s/events" % (self.api, attempt_id),
                {"fencing_token": token, "state": "running"},
                self.headers(),
            )
            output_dir = work / "output"
            output_dir.mkdir()
            timeout = int(attempt.get("max_runtime_seconds") or 3600)
            resources = attempt.get("resources") or {}
            seq_holder = {"seq": 1}

            def on_line(line: str) -> None:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
                http_json(
                    "POST",
                    "%s/attempts/%s/logs" % (self.api, attempt_id),
                    {
                        "fencing_token": token,
                        "first_seq": seq_holder["seq"],
                        "chunks": [
                            {
                                "seq": seq_holder["seq"],
                                "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "stream": "stdout",
                                "line": line,
                            }
                        ],
                    },
                    self.headers(),
                )
                seq_holder["seq"] += 1

            if self.executor == "docker":
                self.note_image_use(resolve_image(attempt.get("image_digest")))
                exit_code, category = run_docker(
                    attempt_id=attempt_id,
                    image_digest=attempt.get("image_digest"),
                    input_dir=src,
                    output_dir=output_dir,
                    entrypoint=attempt["entrypoint"],
                    args=list(attempt.get("args") or []),
                    resources=resources,
                    timeout_seconds=timeout,
                    stop=stop,
                    on_line=on_line,
                    on_stage=tel.push_stage,
                )
            else:
                exit_code, category = run_local_python(
                    python_bin=self.python_bin,
                    input_dir=src,
                    output_dir=output_dir,
                    entrypoint=attempt["entrypoint"],
                    args=list(attempt.get("args") or []),
                    timeout_seconds=timeout,
                    stop=stop,
                    on_line=on_line,
                    on_stage=tel.push_stage,
                )
            tel.push_stage("running", "finished")
            if renew_err and renew_err[0].get("error") in {"fenced", "cancel"}:
                category = "cancelled"
            if category == "cancelled":
                exit_code = exit_code if exit_code not in (None, 0) else 143
                http_json(
                    "POST",
                    "%s/attempts/%s/complete" % (self.api, attempt_id),
                    {
                        "fencing_token": token,
                        "exit_code": exit_code if exit_code is not None else 143,
                        "failure_category": "cancelled",
                        "output_manifest": [],
                    },
                    self.headers(),
                )
                return
            http_json(
                "POST",
                "%s/attempts/%s/events" % (self.api, attempt_id),
                {"fencing_token": token, "state": "uploading"},
                self.headers(),
            )
            manifest = []
            for path in output_dir.rglob("*"):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(output_dir)).replace("\\", "/")
                data = path.read_bytes()
                checksum = hashlib.sha256(data).hexdigest()
                put_headers = dict(fence)
                put_headers["X-Checksum-Sha256"] = checksum
                put_headers["Content-Type"] = "application/octet-stream"
                st, _body = http_bytes(
                    "PUT",
                    "%s/attempts/%s/outputs/%s" % (self.api, attempt_id, rel),
                    data=data,
                    headers=put_headers,
                )
                if st == 413:
                    category = "resource"
                    exit_code = exit_code if exit_code not in (None, 0) else 1
                    break
                if st >= 400:
                    category = category or "internal"
                    break
                manifest.append(
                    {
                        "relative_path": rel,
                        "size_bytes": len(data),
                        "checksum_sha256": checksum,
                    }
                )
            st, payload = http_json(
                "POST",
                "%s/attempts/%s/complete" % (self.api, attempt_id),
                {
                    "fencing_token": token,
                    "exit_code": exit_code,
                    "failure_category": category,
                    "output_manifest": manifest,
                },
                self.headers(),
            )
            print("complete", st, payload, flush=True)
            http_json(
                "POST",
                "%s/attempts/%s/cleanup-complete" % (self.api, attempt_id),
                {"fencing_token": token, "exit_code": exit_code, "output_manifest": []},
                self.headers(),
            )
        except OSError as exc:
            print("execute failed", exc, flush=True)
            http_json(
                "POST",
                "%s/attempts/%s/complete" % (self.api, attempt_id),
                {
                    "fencing_token": token,
                    "exit_code": 1,
                    "failure_category": "internal",
                    "output_manifest": [],
                },
                self.headers(),
            )
        finally:
            stop.set()
            tel.end_job()
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)

    def maybe_update(self, update: dict) -> bool:
        """Install a newer release between jobs. True means exit and restart."""
        version = str(update.get("version") or "")
        if time.time() < self.update_backoff.get(version, 0):
            return False
        if self.release_root is None:
            print("update", version, "available; reinstall with setup-lab-node.sh to enable self-update", flush=True)
            self.update_backoff[version] = time.time() + 3600
            return False
        key = self.state.get("bundle_public_key")
        if not key:
            print("update", version, "skipped: no pinned bundle key", flush=True)
            self.update_backoff[version] = time.time() + 3600
            return False

        def fetch():
            req = urllib.request.Request(self.api + update["url"], headers=self.headers())
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read(), resp.headers

        try:
            installed = updater.apply_update(fetch, update, key, self.release_root, AGENT_VERSION)
        except (updater.UpdateError, OSError, urllib.error.URLError) as exc:
            print("update", version, "failed:", exc, flush=True)
            self.update_backoff[version] = time.time() + 600
            return False
        print("updated to", installed, "- restarting", flush=True)
        return True

    def execute_build(self, build: dict) -> None:
        build_id = build["build_id"]
        print("claimed build", build_id, "tag", build.get("image_tag"), flush=True)
        stop = threading.Event()

        def hb_loop():
            while not stop.wait(max(5, int(self.state.get("lease_ttl_seconds", 30)))):
                self.handle_requests(self.heartbeat()[1])

        t = threading.Thread(target=hb_loop, daemon=True)
        t.start()
        work = None
        try:
            work = attempt_dir("build-" + build_id, self.attempt_root)
            if self.executor != "docker":
                result = {
                    "ok": False,
                    "image_digest": None,
                    "lockfile": "",
                    "log": "docker executor required for image builds",
                    "error": "docker executor required for image builds",
                }
            elif build.get("kind") == "mirror":
                self.note_image_use(build["source_image"], build["image_tag"])
                result = mirror_image(source_image=build["source_image"], image_tag=build["image_tag"])
            else:
                self.note_image_use(resolve_image(build["base_image"]), build["image_tag"])
                result = build_custom_image(
                    base_image=build["base_image"],
                    image_tag=build["image_tag"],
                    requirements=build.get("requirements") or "",
                    need_cuda=bool(build.get("need_cuda")),
                    work_dir=work,
                    build_class=build.get("compat_class"),
                    builder_class=self.compat_class,
                )
            if result.get("ok") and not is_registry_digest(result.get("image_digest")):
                result = {
                    "ok": False,
                    "image_digest": None,
                    "lockfile": result.get("lockfile") or "",
                    "log": (result.get("log") or "") + "\n" + "builder did not return a registry digest",
                    "error": "builder did not return a registry digest",
                }
            st, payload = http_json(
                "POST",
                "%s/agent/builds/%s/complete" % (self.api, build_id),
                {
                    "ok": bool(result.get("ok")),
                    "image_digest": result.get("image_digest"),
                    "lockfile": result.get("lockfile"),
                    "log": (result.get("log") or "")[:200000],
                    "error": result.get("error"),
                },
                self.headers(),
                timeout=60,
            )
            print("build complete", st, payload, flush=True)
        except Exception as exc:
            http_json(
                "POST",
                "%s/agent/builds/%s/complete" % (self.api, build_id),
                {"ok": False, "error": str(exc), "log": str(exc)},
                self.headers(),
            )
        finally:
            stop.set()
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)


def extract_archive(archive_path: Path, dest: Path) -> None:
    data = archive_path.read_bytes()
    if data[:2] == b"PK":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest)
        return
    with tarfile.open(archive_path) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            tf.extractall(dest)


def default_python() -> str:
    return sys.executable


def main() -> None:
    parser = argparse.ArgumentParser(description="Lab ML job agent")
    parser.add_argument("--api", default=None)
    parser.add_argument(
        "--state",
        default=str(Path.home() / ".distributetr9" / "agent-state.json"),
    )
    parser.add_argument("--python", default=default_python())
    parser.add_argument(
        "--executor",
        choices=("auto", "docker", "python"),
        default=None,
        help="auto uses Docker when the daemon is available, otherwise local Python",
    )
    parser.add_argument("--config", default=os.environ.get("LAB_AGENT_CONFIG"))
    parser.add_argument("--cuda-preflight", action="store_true")
    parser.add_argument("--attempt-root", default=os.environ.get("LAB_ATTEMPT_ROOT"))
    parser.add_argument(
        "--role",
        choices=("train", "build"),
        default=None,
        help="train claims GPU jobs; build claims environment builds only",
    )
    args = parser.parse_args()
    cfg = load_config(Path(args.config) if args.config else None)
    api = args.api or cfg.get("api_base") or os.environ.get("LAB_API") or "http://127.0.0.1:8080"
    executor = args.executor or cfg.get("executor") or os.environ.get("LAB_EXECUTOR", "auto")
    role = args.role or cfg.get("role") or os.environ.get("LAB_NODE_ROLE", "train")
    room = cfg.get("room") or os.environ.get("LAB_NODE_ROOM")
    state = Path(args.state)
    install_log_tee(state.parent / LOG_FILE_NAME)
    if cfg.get("node_id") and not state.exists():
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "node_id": cfg.get("node_id"),
                    "node_token": cfg.get("node_token"),
                },
                indent=2,
            )
        )
    print("api", api, flush=True)
    Agent(
        api,
        state,
        args.python,
        executor,
        cuda_test=args.cuda_preflight,
        attempt_root=Path(args.attempt_root) if args.attempt_root else None,
        class_override=cfg.get("compatibility_class"),
        role=role,
        bundle_public_key=cfg.get("bundle_public_key"),
        room=room,
    ).run_logged()


if __name__ == "__main__":
    main()
