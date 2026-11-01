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
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

try:
    from classify import agent_register_fields, disk_free_bytes, gpu_desktop_busy
    from executor import pick_mode, reconcile_lab_containers, run_docker, run_local_python, build_custom_image, is_registry_digest
    import updater
except ImportError:
    from agent.classify import agent_register_fields, disk_free_bytes, gpu_desktop_busy
    from agent.executor import pick_mode, reconcile_lab_containers, run_docker, run_local_python, build_custom_image, is_registry_digest
    from agent import updater

AGENT_VERSION = "0.7.1"


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
    ) -> None:
        self.api = api.rstrip("/")
        self.state_path = state_path
        self.python_bin = python_bin
        self.executor = pick_mode(executor)
        self.cuda_test = cuda_test
        self.attempt_root = attempt_root
        self.class_override = class_override
        self.role = "build" if (role or "train").strip().lower() == "build" else "train"
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
        }
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
        self.save()
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
        return http_json("POST", self.api + "/agent/heartbeat", body, self.headers())

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
        while True:
            try:
                status, hb = self.heartbeat()
                command = (hb or {}).get("command") if isinstance(hb, dict) else None
                if status >= 400:
                    print("heartbeat error", status, hb, flush=True)
                    time.sleep(3)
                    continue
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

    def execute(self, attempt: dict) -> None:
        attempt_id = attempt["attempt_id"]
        token = attempt["fencing_token"]
        print("claimed", attempt_id, "job", attempt["job_id"], flush=True)
        fence = {"X-Fencing-Token": token, **self.headers()}
        stop = threading.Event()
        renew_err = []
        self.heartbeat(attempt_id)

        def renew_loop():
            while not stop.wait(max(5, int(self.state.get("lease_ttl_seconds", 30)) / 3)):
                self.heartbeat(attempt_id)
                st, body = http_json(
                    "POST",
                    "%s/attempts/%s/renew" % (self.api, attempt_id),
                    {"fencing_token": token, "observed_state": "running"},
                    self.headers(),
                )
                if st == 409:
                    renew_err.append(body)
                    stop.set()
                    return
                if (body or {}).get("desired_state") == "cancel":
                    renew_err.append({"error": "cancel"})
                    stop.set()
                    return

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
                )
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
                self.heartbeat()

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
                    "log": "docker executor required for custom requirements.txt",
                    "error": "docker executor required for custom requirements.txt",
                }
            else:
                result = build_custom_image(
                    base_image=build["base_image"],
                    image_tag=build["image_tag"],
                    requirements=build.get("requirements") or "",
                    need_cuda=bool(build.get("need_cuda")),
                    work_dir=work,
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
    state = Path(args.state)
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
    ).run_forever()


if __name__ == "__main__":
    main()
