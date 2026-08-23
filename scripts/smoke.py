#!/usr/bin/env python3
"""Submit the example hello job against a running control API."""
from __future__ import annotations

import json
import os
import socket
import tarfile
import tempfile
import urllib.request
from pathlib import Path

API = os.environ.get("LAB_API", "http://127.0.0.1:8080")
USER = os.environ.get("LAB_USER", "alice@uni.edu")
KIND = os.environ.get("LAB_SMOKE", "hello")
ROOT = Path(__file__).resolve().parents[1]


def lan_api() -> str:
    if os.environ.get("LAB_API"):
        return os.environ["LAB_API"]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    finally:
        sock.close()
    return "http://%s:8080" % ip


def req(method: str, url: str, data=None, headers=None, timeout=30):
    hdrs = {"X-Dev-User": USER, **(headers or {})}
    request = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return resp.status, None
        if "application/json" in resp.headers.get("Content-Type", "") or raw[:1] in (b"{", b"["):
            return resp.status, json.loads(raw.decode())
        return resp.status, raw


def main() -> None:
    print("health", req("GET", API + "/health")[1])
    _st, me = req("GET", API + "/me")
    print("me", me)
    _st, projects = req("GET", API + "/projects")
    if projects["projects"]:
        project_id = projects["projects"][0]["id"]
    else:
        body = json.dumps({"name": "demo"}).encode()
        _st, created = req(
            "POST",
            API + "/projects",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        project_id = created["id"]
    print("project", project_id)
    _st, envs = req("GET", API + "/environments")
    env_id = envs["environments"][0]["id"]
    if KIND == "cuda":
        for row in envs["environments"]:
            if "pytorch" in row["name"]:
                env_id = row["id"]
                break
    example = "cuda" if KIND == "cuda" else "hello"
    src = ROOT / "examples" / example
    tmp = Path(tempfile.mkdtemp())
    archive = tmp / ("%s.tar.gz" % example)
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src / "train.py", arcname="train.py")
    boundary = "----labboundary"
    file_bytes = archive.read_bytes()
    if KIND == "cuda":
        resources = {
            "gpu_class": os.environ.get("LAB_GPU_CLASS", "ampere-8g"),
            "min_vram_mb": 4096,
            "max_runtime_seconds": 300,
        }
    else:
        resources = {"min_vram_mb": 0, "max_runtime_seconds": 120}
    fields = {
        "project_id": project_id,
        "entrypoint": "train.py",
        "environment_id": env_id,
        "args": "[]",
        "resources": json.dumps(resources),
    }
    chunks = []
    for name, value in fields.items():
        chunks.append(("--%s\r\n" % boundary).encode())
        chunks.append(('Content-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (name, value)).encode())
    chunks.append(("--%s\r\n" % boundary).encode())
    chunks.append(
        (
            'Content-Disposition: form-data; name="archive"; filename="%s.tar.gz"\r\n'
            % example
        ).encode()
        + b"Content-Type: application/gzip\r\n\r\n"
    )
    chunks.append(file_bytes)
    chunks.append(("\r\n--%s--\r\n" % boundary).encode())
    body = b"".join(chunks)
    _st, job = req(
        "POST",
        API + "/jobs",
        data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
    )
    print("job", json.dumps(job, indent=2))
    print("\nOn the other desktop:")
    print("  bash agent/run.sh")
    print("  # or: python3 agent/agent.py --api %s --executor python" % lan_api())
    if KIND == "cuda":
        print("GPU smoke: LAB_SMOKE=cuda LAB_GPU_CLASS=ampere-8g python3 scripts/smoke.py")


if __name__ == "__main__":
    main()
