from __future__ import annotations

import io
import json
import tarfile
import uuid


def make_archive(entrypoint: str = "train.py", source: str = "print('ok')\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = source.encode()
        info = tarfile.TarInfo(name=entrypoint)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Portal:
    def __init__(self, client, email: str | None = None) -> None:
        self.client = client
        self.email = email or f"harness-{uuid.uuid4().hex[:8]}@uni.edu"
        self.headers = {"X-Dev-User": self.email}

    def ensure_project(self, name: str | None = None) -> str:
        name = name or f"p-{uuid.uuid4().hex[:8]}"
        resp = self.client.post("/projects", json={"name": name}, headers=self.headers)
        if resp.status_code == 201:
            return resp.json()["id"]
        listed = self.client.get("/projects", headers=self.headers)
        assert listed.status_code == 200, listed.text
        projects = listed.json()["projects"]
        assert projects, listed.text
        return projects[0]["id"]

    def environment_id(self) -> str:
        resp = self.client.get("/environments", headers=self.headers)
        assert resp.status_code == 200, resp.text
        return resp.json()["environments"][0]["id"]

    def submit_job(
        self,
        *,
        min_vram_mb: int = 0,
        gpu_class: str | None = None,
        max_attempts: int | None = None,
        max_runtime_seconds: int = 120,
        requirements: str | None = None,
    ) -> dict:
        project_id = self.ensure_project()
        data = {
            "project_id": project_id,
            "entrypoint": "train.py",
            "environment_id": self.environment_id(),
            "args": "[]",
            "resources": json.dumps(
                {
                    "min_vram_mb": min_vram_mb,
                    "max_runtime_seconds": max_runtime_seconds,
                    "gpu_class": gpu_class,
                }
            ),
        }
        if requirements:
            data["requirements"] = requirements
        files = {"archive": ("hello.tar.gz", make_archive(), "application/gzip")}
        resp = self.client.post("/jobs", data=data, files=files, headers=self.headers)
        assert resp.status_code == 201, resp.text
        job = resp.json()
        if max_attempts is not None:
            # v1 API does not expose max_attempts on create; tests that need it
            # rely on the default of 3 unless patched in DB.
            job["_requested_max_attempts"] = max_attempts
        return job

    def get_job(self, job_id: str) -> dict:
        resp = self.client.get(f"/jobs/{job_id}", headers=self.headers)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def cancel(self, job_id: str) -> dict:
        resp = self.client.post(f"/jobs/{job_id}/cancel", headers=self.headers)
        return {"status_code": resp.status_code, "body": resp.json()}
