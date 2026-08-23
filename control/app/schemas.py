from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MemberAdd(BaseModel):
    email: str
    role: str = "member"


class ResourceSpec(BaseModel):
    gpu_class: str | None = None
    min_vram_mb: int = 0
    min_compute_capability: str | None = None
    cpu_cores: int = 1
    ram_mb: int = 512
    disk_mb: int = 1024
    max_runtime_seconds: int = 3600


class RegisterIn(BaseModel):
    node_id: uuid.UUID | None = None
    hostname: str
    gpu_model: str = "none"
    compatibility_class: str = "cpu"
    vram_total_mb: int = 0
    vram_allocatable_mb: int = 0
    compute_capability: str = "0.0"
    driver_version: str = "unknown"
    windows_version: str = "unknown"
    wsl_version: str = "unknown"
    agent_version: str = "0.1.0"
    cpu_cores: int = 1
    ram_mb: int = 1024
    disk_free_bytes: int = 0
    cuda_preflight_ok: bool = True
    role: str = "train"

    @field_validator("role")
    @classmethod
    def role_must_be_train_or_build(cls, value: str) -> str:
        role = (value or "train").strip().lower()
        if role not in {"train", "build"}:
            raise ValueError("role must be train or build")
        return role


class HeartbeatIn(BaseModel):
    disk_linux_free_bytes: int = 0
    disk_windows_free_bytes: int = 0
    gpu_desktop_busy: bool = False
    current_attempt_id: uuid.UUID | None = None
    agent_healthy: bool = True


class RenewIn(BaseModel):
    fencing_token: uuid.UUID
    observed_state: str | None = None
    resource_usage: dict[str, Any] | None = None


class BuildCompleteIn(BaseModel):
    ok: bool
    image_digest: str | None = None
    lockfile: str | None = None
    log: str | None = None
    error: str | None = None


class EventIn(BaseModel):
    fencing_token: uuid.UUID
    state: str
    at: datetime | None = None


class LogChunk(BaseModel):
    seq: int
    t: str | None = None
    stream: str = "stdout"
    line: str


class LogsIn(BaseModel):
    fencing_token: uuid.UUID
    first_seq: int
    chunks: list[LogChunk]


class OutputManifestItem(BaseModel):
    relative_path: str
    size_bytes: int
    checksum_sha256: str


class CompleteIn(BaseModel):
    fencing_token: uuid.UUID
    exit_code: int = 0
    failure_category: str | None = None
    output_manifest: list[OutputManifestItem] = Field(default_factory=list)


class DrainIn(BaseModel):
    reason: str | None = None


class QuarantineIn(BaseModel):
    reason: str
