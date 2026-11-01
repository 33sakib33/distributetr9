from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class MemberAdd(BaseModel):
    email: str
    role: Literal["owner", "member"] = "member"


class MemberRoleIn(BaseModel):
    role: Literal["owner", "member"]


class ResourceSpec(BaseModel):
    gpu_class: str | None = None
    min_vram_mb: int = 0
    min_compute_capability: str | None = None
    cpu_cores: int = 1
    ram_mb: int = 512
    disk_mb: int = 1024
    max_runtime_seconds: int = 3600


ROOM_MAX = 64
LABEL_MAX = 32
LABELS_MAX = 20

RESTART_REASONS = {"first_start", "update", "crash", "reboot", "manual"}


class LastRestart(BaseModel):
    reason: str = "manual"
    exit_code: int | None = None
    detail: str | None = Field(default=None, max_length=8000)
    at: str | None = None

    @field_validator("reason")
    @classmethod
    def known_reason(cls, value: str) -> str:
        return value if value in RESTART_REASONS else "manual"


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
    last_restart: LastRestart | None = None
    room: str | None = None

    @field_validator("room")
    @classmethod
    def tidy_room(cls, value: str | None) -> str | None:
        # A long or blank room in the install config must not stop a PC registering.
        return (value or "").strip()[:ROOM_MAX] or None

    @field_validator("role")
    @classmethod
    def role_must_be_train_or_build(cls, value: str) -> str:
        role = (value or "train").strip().lower()
        if role not in {"train", "build"}:
            raise ValueError("role must be train or build")
        return role


class HealthIn(BaseModel):
    gpu_temp_c: float | None = None
    gpu_pct: float | None = None
    vram_used_mb: int | None = None
    driver_version: str | None = None
    uptime_s: int | None = None
    docker_ok: bool | None = None


class HeartbeatIn(BaseModel):
    disk_linux_free_bytes: int = 0
    disk_windows_free_bytes: int = 0
    gpu_desktop_busy: bool = False
    current_attempt_id: uuid.UUID | None = None
    agent_healthy: bool = True
    health: HealthIn | None = None


class UsageIn(BaseModel):
    gpu_pct: float | None = None
    vram_mb: float | None = None
    cpu_pct: float | None = None
    ram_mb: float | None = None


class StageIn(BaseModel):
    name: str
    state: Literal["started", "finished"]
    progress: dict[str, Any] | None = None


class RenewIn(BaseModel):
    fencing_token: uuid.UUID
    observed_state: str | None = None
    # Log counters; the server keeps its own meaning for this (see logs.py).
    resource_usage: dict[str, Any] | None = None
    # CPU/GPU/RAM sample for the graphs.
    usage: UsageIn | None = None
    # Stage changes since the last renew (an agent may send one or several).
    stage: StageIn | None = None
    stages: list[StageIn] = Field(default_factory=list, max_length=20)


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


class RingIn(BaseModel):
    ring: int = Field(ge=0, le=9)


class MetaIn(BaseModel):
    """Admin edit of a PC's room and labels; a field left out is not touched,
    an empty room clears it."""

    room: str | None = None
    labels: list[str] | None = None

    @field_validator("room")
    @classmethod
    def tidy_room(cls, value: str | None) -> str | None:
        room = (value or "").strip()
        if len(room) > ROOM_MAX:
            raise ValueError(f"room is longer than {ROOM_MAX} characters")
        return room or None

    @field_validator("labels")
    @classmethod
    def tidy_labels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        labels = list(dict.fromkeys(label.strip() for label in value if label.strip()))
        if len(labels) > LABELS_MAX:
            raise ValueError(f"at most {LABELS_MAX} labels")
        if any(len(label) > LABEL_MAX for label in labels):
            raise ValueError(f"a label is longer than {LABEL_MAX} characters")
        return labels


class BulkCancelIn(BaseModel):
    job_ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class MuteIn(BaseModel):
    minutes: int = Field(ge=1, le=60 * 24 * 30)


class AckKindIn(BaseModel):
    kind: str = Field(min_length=1, max_length=32)


def _tidy_room(value: str | None) -> str | None:
    room = (value or "").strip()
    if len(room) > ROOM_MAX:
        raise ValueError(f"room is longer than {ROOM_MAX} characters")
    return room or None


def _tidy_note(value: str | None) -> str | None:
    return (value or "").strip() or None


def _strip_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def _tidy_archs(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    archs = list(dict.fromkeys(a.strip() for a in value if a.strip()))
    if not archs:
        raise ValueError("at least one architecture")
    return archs


class ScheduleIn(BaseModel):
    """A weekly closed window for a room or one PC. Times are minutes since
    midnight in LAB_TIMEZONE; a window never crosses midnight (split it)."""

    room: str | None = None
    node_id: uuid.UUID | None = None
    weekday: int = Field(ge=0, le=6)
    start_minute: int = Field(ge=0, le=1439)
    end_minute: int = Field(ge=1, le=1440)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("room")
    @classmethod
    def tidy_room(cls, value: str | None) -> str | None:
        return _tidy_room(value)

    @field_validator("note")
    @classmethod
    def tidy_note(cls, value: str | None) -> str | None:
        return _tidy_note(value)


class SchedulePatch(BaseModel):
    room: str | None = None
    node_id: uuid.UUID | None = None
    weekday: int | None = Field(default=None, ge=0, le=6)
    start_minute: int | None = Field(default=None, ge=0, le=1439)
    end_minute: int | None = Field(default=None, ge=1, le=1440)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("room")
    @classmethod
    def tidy_room(cls, value: str | None) -> str | None:
        return _tidy_room(value)

    @field_validator("note")
    @classmethod
    def tidy_note(cls, value: str | None) -> str | None:
        return _tidy_note(value)


class QuotaIn(BaseModel):
    max_concurrent_jobs: int = Field(ge=0, le=1000)


class EnvironmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    framework: str = Field(min_length=1, max_length=40)
    python_version: str = Field(min_length=1, max_length=20)
    cuda_version: str = Field(min_length=1, max_length=20)
    gpu_archs: list[str] = Field(min_length=1, max_length=20)
    image_digest: str = Field(min_length=1, max_length=500)

    @field_validator("framework", "python_version", "cuda_version", "image_digest")
    @classmethod
    def strip_text(cls, value: str) -> str | None:
        return _strip_text(value)

    @field_validator("gpu_archs")
    @classmethod
    def tidy_archs(cls, value: list[str]) -> list[str] | None:
        return _tidy_archs(value)


class EnvironmentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    framework: str | None = Field(default=None, min_length=1, max_length=40)
    python_version: str | None = Field(default=None, min_length=1, max_length=20)
    cuda_version: str | None = Field(default=None, min_length=1, max_length=20)
    gpu_archs: list[str] | None = Field(default=None, min_length=1, max_length=20)
    image_digest: str | None = Field(default=None, min_length=1, max_length=500)
    retired: bool | None = None

    @field_validator("framework", "python_version", "cuda_version", "image_digest")
    @classmethod
    def strip_text(cls, value: str | None) -> str | None:
        return _strip_text(value)

    @field_validator("gpu_archs")
    @classmethod
    def tidy_archs(cls, value: list[str] | None) -> list[str] | None:
        return _tidy_archs(value)


class RolloutIn(BaseModel):
    """A field left out is not touched; null (or an empty string) clears the
    override and brings back the environment default."""

    target_version: str | None = Field(default=None, max_length=40)
    rollout_ring: int | None = Field(default=None, ge=0, le=9)
    min_version: str | None = Field(default=None, max_length=40)


class CloneIn(BaseModel):
    """What may differ in a cloned job; everything left out is copied.
    `resources` keys replace the original's one by one."""

    resources: dict[str, Any] | None = None
    args: list[Any] | None = None
    entrypoint: str | None = Field(default=None, max_length=500)
    environment_id: uuid.UUID | None = None


class NotificationsIn(BaseModel):
    notify_email: bool | None = None
    notify_webhook_url: str | None = Field(default=None, max_length=500)

    @field_validator("notify_webhook_url")
    @classmethod
    def webhook_is_http(cls, value: str | None) -> str | None:
        url = (value or "").strip()
        if not url:
            return None
        if not re.fullmatch(r"https?://[^\s/]+\S*", url):
            raise ValueError("the webhook must be an http:// or https:// URL")
        return url


class ClientErrorIn(BaseModel):
    """An error caught in the portal. Long values are cut, not refused: a report
    that is too big is still worth having."""

    message: str = ""
    stack: str = ""
    url: str = ""
    user_agent: str = ""

    @field_validator("message", "stack", "url", "user_agent", mode="before")
    @classmethod
    def _text(cls, value: Any, info) -> str:
        limit = {"message": 2000, "stack": 8000, "url": 500, "user_agent": 300}[info.field_name]
        return "" if value is None else str(value)[:limit]
