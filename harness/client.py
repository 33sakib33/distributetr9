from __future__ import annotations

import uuid


GPU_CLASSES = (
    {"compatibility_class": "cpu", "gpu_model": "none", "vram_total_mb": 0, "vram_allocatable_mb": 0},
    {
        "compatibility_class": "ada-12g",
        "gpu_model": "NVIDIA GeForce RTX 4070",
        "vram_total_mb": 12282,
        "vram_allocatable_mb": 12000,
    },
    {
        "compatibility_class": "ada-16g",
        "gpu_model": "NVIDIA GeForce RTX 4080",
        "vram_total_mb": 16376,
        "vram_allocatable_mb": 16000,
    },
)


class FakeAgent:
    def __init__(self, client, *, hostname: str, caps: dict | None = None, role: str = "train") -> None:
        self.client = client
        self.hostname = hostname
        self.caps = caps or GPU_CLASSES[0]
        self.role = role
        self.node_id: str | None = None
        self.token: str | None = None
        self.boot_session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        assert self.token and self.node_id and self.boot_session_id
        return {
            "Authorization": f"Bearer {self.token}",
            "X-Node-Id": self.node_id,
            "X-Boot-Session-Id": self.boot_session_id,
        }

    def register(self, *, reuse: bool = True) -> dict:
        body = {
            "node_id": self.node_id if reuse else None,
            "hostname": self.hostname,
            "gpu_model": self.caps["gpu_model"],
            "compatibility_class": self.caps["compatibility_class"],
            "vram_total_mb": self.caps["vram_total_mb"],
            "vram_allocatable_mb": self.caps["vram_allocatable_mb"],
            "compute_capability": self.caps.get(
                "compute_capability", "8.9" if self.caps["vram_allocatable_mb"] else "0.0"
            ),
            "driver_version": "test",
            "windows_version": "test",
            "wsl_version": "test",
            "agent_version": self.caps.get("agent_version", "harness-0.1"),
            "cpu_cores": 8,
            "ram_mb": 16000,
            "disk_free_bytes": 200_000_000_000,
            "cuda_preflight_ok": True,
            "role": self.role,
        }
        headers = {}
        if reuse and self.token and self.node_id:
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            body["node_id"] = None
        resp = self.client.post("/agent/register", json=body, headers=headers)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        self.node_id = payload["node_id"]
        self.token = payload["node_token"]
        self.boot_session_id = payload["boot_session_id"]
        return payload

    def heartbeat(self, attempt_id: str | None = None) -> dict:
        resp = self.client.post(
            "/agent/heartbeat",
            json={
                "disk_linux_free_bytes": 200_000_000_000,
                "disk_windows_free_bytes": 200_000_000_000,
                "gpu_desktop_busy": False,
                "current_attempt_id": attempt_id,
                "agent_healthy": True,
            },
            headers=self._headers(),
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    def claim(self) -> dict | None:
        resp = self.client.post("/agent/claim", json={}, headers=self._headers())
        assert resp.status_code == 200, resp.text
        return resp.json().get("attempt")

    def acknowledge(self, attempt: dict, state: str = "starting") -> dict:
        resp = self.client.post(
            f"/attempts/{attempt['attempt_id']}/acknowledge",
            json={"fencing_token": attempt["fencing_token"], "state": state},
            headers=self._headers(),
        )
        return {"status_code": resp.status_code, "body": resp.json()}

    def event(self, attempt: dict, state: str) -> dict:
        resp = self.client.post(
            f"/attempts/{attempt['attempt_id']}/events",
            json={"fencing_token": attempt["fencing_token"], "state": state},
            headers=self._headers(),
        )
        return {"status_code": resp.status_code, "body": resp.json()}

    def renew(self, attempt: dict, state: str = "running") -> dict:
        resp = self.client.post(
            f"/attempts/{attempt['attempt_id']}/renew",
            json={"fencing_token": attempt["fencing_token"], "observed_state": state},
            headers=self._headers(),
        )
        return {"status_code": resp.status_code, "body": resp.json()}

    def complete(
        self,
        attempt: dict,
        *,
        exit_code: int = 0,
        failure_category: str | None = None,
        fencing_token: str | None = None,
    ) -> dict:
        token = fencing_token or attempt["fencing_token"]
        resp = self.client.post(
            f"/attempts/{attempt['attempt_id']}/complete",
            json={
                "fencing_token": token,
                "exit_code": exit_code,
                "failure_category": failure_category,
                "output_manifest": [],
            },
            headers=self._headers(),
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "body": body}

    def put_output(self, attempt: dict, relative_path: str, data: bytes) -> dict:
        checksum = __import__("hashlib").sha256(data).hexdigest()
        resp = self.client.put(
            f"/attempts/{attempt['attempt_id']}/outputs/{relative_path}",
            content=data,
            headers={
                **self._headers(),
                "X-Fencing-Token": attempt["fencing_token"],
                "X-Checksum-Sha256": checksum,
                "Content-Type": "application/octet-stream",
            },
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "body": body, "checksum": checksum}

    def post_logs(self, attempt: dict, lines: list[str], *, first_seq: int = 1) -> dict:
        chunks = [
            {
                "seq": first_seq + i,
                "t": "2026-01-01T00:00:00Z",
                "stream": "stdout",
                "line": line,
            }
            for i, line in enumerate(lines)
        ]
        resp = self.client.post(
            f"/attempts/{attempt['attempt_id']}/logs",
            json={
                "fencing_token": attempt["fencing_token"],
                "first_seq": first_seq,
                "chunks": chunks,
            },
            headers=self._headers(),
        )
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        return {"status_code": resp.status_code, "body": body}

    def run_to_complete(self, attempt: dict) -> dict:
        self.acknowledge(attempt)
        self.event(attempt, "running")
        self.event(attempt, "uploading")
        return self.complete(attempt)


def stale_token() -> str:
    return str(uuid.uuid4())
