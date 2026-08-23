# 02 — Data model

PostgreSQL holds metadata. MinIO holds bytes. Every artifact row points at an object key, size, and checksum.

Use UUIDv7 (or UUID + `created_at`) for new IDs so listings sort by time without extra columns.

## Enums

```text
node_eligibility:     eligible | draining | quarantined | offline
job_desired_state:    run | cancel
attempt_state:        leased | starting | running | uploading | completed | failed | cancelling | cancelled | unknown
failure_category:     user_code | environment | resource | cancelled | lease_lost | node_lost | timeout | internal
artifact_type:        input | output | log | checkpoint | executed_notebook
environment_state:    pending | building | testing | ready | failed
member_role:          owner | member
admin_role:           admin | operator
```

User-visible job status is **derived**, not stored as a free-form string. See [03-state-machine.md](03-state-machine.md).

## Tables

### users

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| university_sub | text unique not null | IdP subject |
| email | text not null | |
| display_name | text not null | |
| is_admin | bool not null default false | |
| disabled_at | timestamptz | |
| created_at | timestamptz not null | |

### projects

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| name | text not null | Unique per owner in v1 |
| created_by | uuid fk users | |
| created_at | timestamptz not null | |

### project_members

| Column | Type | Notes |
|--------|------|--------|
| project_id | uuid fk | |
| user_id | uuid fk | |
| role | member_role not null | |
| pk | (project_id, user_id) | |

### quotas

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| subject_type | text not null | `user` or `project` |
| subject_id | uuid not null | |
| max_concurrent_jobs | int not null | |

Unique `(subject_type, subject_id)`. Defaults: user=1, project=2 until M12 says otherwise.

### nodes

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | Stable across reboots |
| boot_session_id | text not null | Changes every WSL/agent start |
| hostname | text not null | |
| gpu_model | text not null | |
| compatibility_class | text not null | From M03 inventory |
| vram_total_mb | int not null | |
| vram_allocatable_mb | int not null | |
| compute_capability | text not null | e.g. `8.9` |
| driver_version | text not null | |
| windows_version | text not null | |
| wsl_version | text not null | |
| agent_version | text not null | |
| cpu_cores | int not null | |
| ram_mb | int not null | |
| disk_free_bytes | bigint not null | Linux VHD + Windows volume tracked in heartbeat JSON too |
| last_heartbeat_at | timestamptz | Server-observed |
| eligibility | node_eligibility not null | |
| eligibility_reason | text | Drain or quarantine reason |
| gpu_desktop_busy | bool not null default false | Host is using the GPU |
| registered_at | timestamptz not null | |

Heartbeat payload (GPU health, both disks, driver) is stored in `node_heartbeats` or as `jsonb last_health`. Keep a short history (last 24h) for ops; do not log every poll forever.

### node_credentials

| Column | Type | Notes |
|--------|------|--------|
| node_id | uuid fk | |
| token_hash | text not null | Rotate in M13 |
| created_at | timestamptz not null | |
| revoked_at | timestamptz | |

### jobs

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| user_id | uuid fk not null | |
| project_id | uuid fk not null | |
| submission_artifact_id | uuid fk artifacts | Input archive |
| entrypoint | text not null | Path inside archive, `.py` only in v1 |
| args | jsonb not null default `[]` | List of strings |
| environment_id | uuid fk environments | Catalog env in v1 |
| resources | jsonb not null | See below |
| priority | int not null default 0 | Higher runs sooner |
| max_attempts | int not null default 3 | |
| max_runtime_seconds | int not null | |
| desired_state | job_desired_state not null default `run` | |
| accepted_attempt_id | uuid fk job_attempts | Set only when a result is accepted |
| pending_reason | text | Human-readable queue explanation |
| created_at | timestamptz not null | |
| completed_at | timestamptz | |

`resources` shape:

```json
{
  "gpu_class": "rtx_4070",
  "min_vram_mb": 12288,
  "min_compute_capability": "8.0",
  "cpu_cores": 4,
  "ram_mb": 16384,
  "disk_mb": 20480,
  "max_runtime_seconds": 14400
}
```

### job_attempts

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| job_id | uuid fk not null | |
| attempt_number | int not null | 1-based, unique per job |
| node_id | uuid fk not null | |
| fencing_token | uuid not null unique | |
| lease_expires_at | timestamptz not null | |
| observed_state | attempt_state not null | |
| exit_code | int | |
| failure_category | failure_category | |
| output_namespace | text not null | `jobs/{job_id}/attempts/{attempt_id}/` |
| last_log_seq | bigint not null default 0 | |
| resource_usage | jsonb | cpu, rss, gpu_util, disk |
| started_at | timestamptz | |
| completed_at | timestamptz | |
| created_at | timestamptz not null | |

Partial unique index: at most one row per `node_id` where `observed_state` is in `leased, starting, running, uploading, cancelling, unknown`.

### environments

Catalog of maintained images.

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| name | text unique not null | e.g. `pytorch-stable-cu124` |
| framework | text not null | `pytorch` \| `tensorflow` |
| python_version | text not null | |
| cuda_version | text not null | |
| gpu_archs | text[] not null | |
| image_digest | text not null | Immutable |
| lockfile | text | Recorded snapshot |
| cuda_preflight_ok | bool not null | |
| created_at | timestamptz not null | |

### environment_builds

Used by M09. Present in the schema from day one so the portal can show `building` later without a migration surprise. v1 may have zero rows.

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| environment_hash | text unique not null | spec + base digest + compat class |
| state | environment_state not null | |
| base_image | text not null | |
| lockfile | text | |
| image_digest | text | |
| checksum | text | |
| created_at | timestamptz not null | |
| finished_at | timestamptz | |

Build logs live in MinIO, referenced by artifacts of type `log` with `job_id` null and `build_id` set. Add `build_id` nullable on `artifacts` when M09 starts.

### artifacts

| Column | Type | Notes |
|--------|------|--------|
| id | uuid pk | |
| job_id | uuid fk | Null for env-build logs |
| attempt_id | uuid fk | Null for submission inputs |
| type | artifact_type not null | |
| object_key | text not null unique | |
| size_bytes | bigint not null | |
| checksum_sha256 | text not null | |
| created_at | timestamptz not null | |

Do not store log bodies here.

## Object-storage key layout

```text
submissions/{job_id}/input.tar.gz
jobs/{job_id}/attempts/{attempt_id}/logs/{seq}.jsonl.gz
jobs/{job_id}/attempts/{attempt_id}/outputs/{relative_path}
jobs/{job_id}/attempts/{attempt_id}/checkpoints/{name}     # later
jobs/{job_id}/promoted/{relative_path}                     # copy after fencing check
builds/{build_id}/logs/{seq}.jsonl.gz                      # later
```

Final user download reads `promoted/` only after the scheduler accepts the attempt. Attempt namespaces stay immutable so a fenced-out attempt cannot overwrite a winner.

## Derived job status

Computed from `jobs.desired_state`, `jobs.accepted_attempt_id`, and the latest attempt:

| Condition | User-visible status |
|-----------|---------------------|
| No attempt, desired=run, validating inputs | `validating` |
| No attempt, desired=run | `queued` |
| Latest attempt leased/starting, not yet running | `starting` (portal may show `leased` to admins) |
| Latest attempt running | `running` |
| Latest attempt uploading | `uploading` |
| accepted_attempt completed | `completed` |
| accepted_attempt failed, retries remaining, desired=run | `queued` (retrying) |
| accepted_attempt failed, no retries | `failed` |
| desired=cancel, not yet terminal | `cancelling` |
| accepted_attempt cancelled or cancel completed with no running attempt | `cancelled` |
| Latest attempt unknown | `unknown` |

## Indexes

- `jobs (project_id, created_at desc)`
- `jobs (user_id, created_at desc)`
- `jobs (desired_state, created_at)` where `accepted_attempt_id is null`
- `job_attempts (job_id, attempt_number unique)`
- `job_attempts (node_id, observed_state)`
- `job_attempts (lease_expires_at)` where state is active
- `nodes (eligibility, compatibility_class)`
- `artifacts (job_id, type)`
