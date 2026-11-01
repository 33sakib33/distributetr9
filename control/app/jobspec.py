"""Checks shared by the routes that create a job (submit and clone)."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.errors import AppError
from app.models import User


def job_resources(res: dict[str, Any], user: User) -> tuple[dict[str, Any], int]:
    """Validate a resource request; returns the stored `resources` dict and the
    maximum runtime in seconds."""
    try:
        runtime = int(res.get("max_runtime_seconds") or 3600)
        min_vram = int(res.get("min_vram_mb") or 0)
        resources = {
            "gpu_class": res.get("gpu_class"),
            "min_vram_mb": min_vram,
            "min_compute_capability": res.get("min_compute_capability"),
            "cpu_cores": int(res.get("cpu_cores") or 1),
            "ram_mb": int(res.get("ram_mb") or 512),
            "disk_mb": int(res.get("disk_mb") or 1024),
            "max_runtime_seconds": runtime,
        }
    except (TypeError, ValueError) as exc:
        raise AppError(400, "invalid", "resource values must be numbers") from exc
    if runtime <= 0 or runtime > settings.global_max_runtime_seconds:
        raise AppError(400, "invalid", "max_runtime_seconds out of range")
    if min_vram > 24576 and not user.is_admin:
        raise AppError(400, "invalid", "min_vram_mb exceeds v1 cap")
    return resources, runtime


def waiting_reason(min_vram_mb: int) -> str:
    return "waiting for a matching GPU" if min_vram_mb else "waiting for a matching node"
