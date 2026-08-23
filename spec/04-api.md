# 04 — API

Two surfaces, one FastAPI app. Portal routes require a user session. Agent routes require a node token. Every mutating agent route is idempotent.

Use problem+json errors:

```json
{ "error": "fenced", "message": "stale fencing token", "job_id": "...", "attempt_id": "..." }
```

## Auth

| Client | Mechanism |
|--------|-----------|
| Human | University OIDC. Dev fallback: `DEV_AUTH=1` header `X-Dev-User`. |
| Agent | `Authorization: Bearer <node_token>` plus `X-Node-Id` and `X-Boot-Session-Id` |
| Admin portal routes | Same user session; `users.is_admin` |

## Portal API

### POST /jobs

Create a job. Multipart: `archive` file plus JSON fields.

Request fields:

```json
{
  "project_id": "uuid",
  "entrypoint": "train.py",
  "args": ["--epochs", "10"],
  "environment_id": "uuid",
  "resources": {
    "gpu_class": null,
    "min_vram_mb": 12288,
    "min_compute_capability": null,
    "cpu_cores": 4,
    "ram_mb": 16384,
    "disk_mb": 20480,
    "max_runtime_seconds": 14400
  }
}
```

Validation: `.py` entrypoint, archive zip or tar.gz, size limit (start 500 MB), member of project, environment exists and `cuda_preflight_ok`, resources within global caps.

Response 201: job object including derived `status` and `pending_reason`.

### GET /jobs

Query: `project_id`, `status`, `cursor`. Own jobs, or project membership.

### GET /jobs/{job_id}

Full job, latest attempt, timings:

```json
{
  "id": "...",
  "status": "running",
  "pending_reason": null,
  "timings": {
    "queued_ms": 12000,
    "starting_ms": 4000,
    "running_ms": 90000,
    "uploading_ms": 0
  },
  "accepted_attempt_id": null,
  "latest_attempt": { }
}
```

v1 timings omit `building_ms` or return 0.

### POST /jobs/{job_id}/cancel

Sets `desired_state=cancel`. Idempotent. 200 if already cancelling/cancelled. 409 if already completed.

### POST /jobs/{job_id}/retry

Allowed only when job is `failed` and `desired_state` is not `cancel`. Creates a fresh job-level retry: `desired_state=run`, `accepted_attempt_id=null`, increments a retry counter without cloning user errors into a new job id. Same job id, new attempt. 409 if retries exhausted or job is not failed.

Checkpoint resume is later; v1 retry reruns from the original submission.

### GET /jobs/{job_id}/logs

Query: `attempt_id` (default latest), `after_seq`. Returns chunks `{seq, t, stream, line}`. Also available as SSE `GET /jobs/{job_id}/logs/stream`.

### GET /jobs/{job_id}/artifacts

Lists promoted artifacts for an accepted attempt, plus attempt-scoped logs. Download via `GET /artifacts/{artifact_id}/download` (presigned redirect to MinIO).

### GET /nodes

Admin. List nodes, eligibility, last heartbeat, current attempt.

### POST /nodes/{node_id}/drain

Admin. `eligibility=draining`. Node finishes current attempt, claims nothing new.

### POST /nodes/{node_id}/quarantine

Admin. Body `{ "reason": "..." }`. Current attempt is cancelled by scheduler as `node_lost` if still active, then node is ineligible.

### GET /environments

Catalog for the submit form.

## Agent API

All bodies include `fencing_token` when an `attempt_id` is in the path (except register, heartbeat, claim).

### POST /agent/register

```json
{
  "node_id": null,
  "hostname": "lab-12",
  "gpu_model": "NVIDIA GeForce RTX 4070",
  "compatibility_class": "ada-12g",
  "vram_total_mb": 12282,
  "vram_allocatable_mb": 12000,
  "compute_capability": "8.9",
  "driver_version": "560.xx",
  "windows_version": "...",
  "wsl_version": "...",
  "agent_version": "0.1.0",
  "cpu_cores": 16,
  "ram_mb": 32000,
  "disk_free_bytes": 200000000000,
  "cuda_preflight_ok": true
}
```

Response: `{ "node_id", "node_token", "lease_ttl_seconds", "heartbeat_interval_seconds", "disconnect_grace_seconds" }`. Re-register with existing `node_id` + valid token rotates `boot_session_id` and fences old-session attempts.

### POST /agent/heartbeat

```json
{
  "disk_linux_free_bytes": 0,
  "disk_windows_free_bytes": 0,
  "gpu_desktop_busy": false,
  "current_attempt_id": null,
  "agent_healthy": true
}
```

Response may include commands: `continue` | `drain` | `quarantine` | `cancel_attempt` | `self_terminate`.

### POST /agent/claim

Long-poll up to 25s. Empty `{ "attempt": null }` or a claim:

```json
{
  "attempt": {
    "attempt_id": "uuid",
    "job_id": "uuid",
    "fencing_token": "uuid",
    "lease_expires_at": "rfc3339",
    "image_digest": "sha256:...",
    "entrypoint": "train.py",
    "args": [],
    "resources": {},
    "input": { "artifact_id": "...", "url": "presigned" },
    "output_namespace": "jobs/.../attempts/.../",
    "log_upload": { "url_template": "..." },
    "max_runtime_seconds": 14400
  }
}
```

Idempotent: repeat claim returns the same active attempt for this node.

### POST /attempts/{id}/acknowledge

Agent has the claim locally and is staging. Transition `leased → starting` is allowed here or via events. Prefer: acknowledge = claim received; `events` carries `starting` / `running`.

### POST /attempts/{id}/renew

```json
{ "fencing_token": "uuid", "observed_state": "running", "resource_usage": {} }
```

Response: `{ "lease_expires_at", "desired_state": "run"|"cancel" }`.

### POST /attempts/{id}/events

```json
{ "fencing_token": "uuid", "state": "running", "at": "rfc3339" }
```

Only legal transitions from [03-state-machine.md](03-state-machine.md).

### POST /attempts/{id}/logs

```json
{
  "fencing_token": "uuid",
  "first_seq": 41,
  "chunks": [{ "seq": 41, "t": "...", "stream": "stdout", "line": "epoch 1" }]
}
```

Reject if `first_seq` is neither `last_log_seq+1` nor a full retry of an already stored range (idempotent overlap). Enforce rate and size limits (M10).

### POST /attempts/{id}/complete

```json
{
  "fencing_token": "uuid",
  "exit_code": 0,
  "failure_category": null,
  "output_manifest": [
    { "relative_path": "model.pt", "size_bytes": 1, "checksum_sha256": "..." }
  ]
}
```

Scheduler verifies fencing, desired state, and that output objects exist with matching checksums before promoting. Idempotent as defined in the state machine.

### POST /attempts/{id}/cleanup-complete

Agent has removed the container and attempt directories. Informational; may arrive after the job is already terminal.

## Live logs

Portal uses SSE (`text/event-stream`) or WebSockets. v1: SSE. Event: `{ "seq", "stream", "line" }`. Reconnect with `Last-Event-ID` = last seq.

## Idempotency keys

Agent mutating routes also accept `Idempotency-Key`. If present, store response for 24h keyed by `(node_id, key)`. Claim, complete, cancel, renew must work even without the header, using natural keys (node active attempt, fencing token).
