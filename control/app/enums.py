from __future__ import annotations

import enum

ACTIVE_ATTEMPT_STATES = (
    "leased",
    "starting",
    "running",
    "uploading",
    "cancelling",
    "unknown",
)
TERMINAL_ATTEMPT_STATES = ("completed", "failed", "cancelled")
RETRYABLE_FAILURES = ("lease_lost", "node_lost", "internal")

FORWARD_TRANSITIONS = {
    "leased": {"starting", "running", "uploading", "failed", "cancelling", "unknown"},
    "starting": {"running", "uploading", "failed", "cancelling", "unknown"},
    "running": {"uploading", "failed", "cancelling", "unknown"},
    "uploading": {"completed", "failed", "cancelling", "unknown"},
    "cancelling": {"cancelled", "unknown", "failed"},
    "unknown": {"running", "starting", "uploading", "leased", "failed", "cancelled", "cancelling"},
}


class NodeEligibility(str, enum.Enum):
    eligible = "eligible"
    draining = "draining"
    quarantined = "quarantined"
    offline = "offline"


class JobDesiredState(str, enum.Enum):
    run = "run"
    cancel = "cancel"


class AttemptState(str, enum.Enum):
    leased = "leased"
    starting = "starting"
    running = "running"
    uploading = "uploading"
    completed = "completed"
    failed = "failed"
    cancelling = "cancelling"
    cancelled = "cancelled"
    unknown = "unknown"


class FailureCategory(str, enum.Enum):
    user_code = "user_code"
    environment = "environment"
    resource = "resource"
    cancelled = "cancelled"
    lease_lost = "lease_lost"
    node_lost = "node_lost"
    timeout = "timeout"
    internal = "internal"


class ArtifactType(str, enum.Enum):
    input = "input"
    output = "output"
    log = "log"
    checkpoint = "checkpoint"
    executed_notebook = "executed_notebook"


class EnvironmentState(str, enum.Enum):
    pending = "pending"
    building = "building"
    testing = "testing"
    ready = "ready"
    failed = "failed"


class MemberRole(str, enum.Enum):
    owner = "owner"
    member = "member"
