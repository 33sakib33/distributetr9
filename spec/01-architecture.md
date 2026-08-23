# 01 — Architecture

Option A: WSL2 with Docker Engine. No Docker Desktop.

```text
Portal frontend (Next.js)
    │
    ▼
FastAPI control API
    ├── Identity
    ├── Scheduler and lease service
    ├── Environment build service   (stub in v1; full in M09)
    ├── Log ingestion
    └── Artifact service
           │
           ├── PostgreSQL      metadata only
           ├── MinIO           submissions, logs, outputs, checkpoints
           └── Container registry

Windows supervisor on every PC
    │
    ▼
WSL agent
    └── Docker container executor
```

## Components

| Component | Runs where | Responsibility |
|-----------|------------|----------------|
| Portal | Browser / Next.js | Submit, list, detail, live logs, cancel, retry, admin node controls |
| Control API | Lab server | Auth, jobs, scheduling, agent protocol, artifacts, admin |
| Scheduler | Same process as Control API in v1 | Claim matching, leases, fencing, retries, derived job status |
| PostgreSQL | Lab server | Users, jobs, attempts, nodes, leases, quotas, artifact metadata |
| MinIO | Lab server | Object bytes. Never store these in the database |
| Registry | Lab server | Maintained images by immutable digest |
| Windows supervisor | Each PC, Windows service | Boot WSL, keep agent alive, host GPU/disk health, reboot status |
| WSL agent | Each PC, inside WSL | Register, long-poll, stage, execute, renew, upload, fence, clean |
| Docker Engine | Each PC, inside WSL | One container per attempt |
| Fake-agent harness | CI / dev | 100 simulated agents; concurrency and race tests |

v1 may run scheduler, log ingestion, and artifact service inside one FastAPI app. Keep module boundaries in code so they can split later. Do not put agent execution logic in the control API.

## Runtime layout on a PC

- Dedicated Windows service account (not the logged-in student).
- Supervisor starts at boot, even with no interactive login.
- One imported, managed WSL distro per PC.
- Docker Engine or containerd inside WSL.
- One training job per physical consumer GPU. v1: one GPU PC ⇒ one job.
- Read-only container image.
- Read-only input and dataset mounts.
- Writable attempt-specific output, cache, and tmp.
- No Docker socket and no agent credentials inside the job container.
- Entire container removed after the attempt finishes.

## Networks and trust

- Agents pull work. They long-poll the Control API. The control plane never SSHs into a PC to start a job.
- Node credentials are per-node, rotatable, and never mounted into job containers.
- Campus-local registry and MinIO. Agents pull images and artifacts over the lab network (Tailscale is the expected overlay; the spec does not depend on a specific VPN brand).

## Storage split

| Kind | Store |
|------|--------|
| Users, jobs, attempts, nodes, leases, quotas, artifact metadata, build records | PostgreSQL |
| Submission archives, log chunks, outputs, future checkpoints | MinIO |
| Container images | Registry, plus a local image cache on each PC |

## v1 execution path

1. User uploads an archive and submits a job against a catalog environment.
2. Control API validates the archive, records the job, puts it in `queued` (no custom image build in v1).
3. An eligible agent claims the job. Scheduler writes an attempt, fencing token, and lease.
4. Agent pulls the image digest, stages input, starts one container with GPU + limits.
5. Agent renews the lease, streams log chunks, then uploads outputs and reports completion with the fencing token.
6. Scheduler accepts at most one completion. Stale tokens are rejected. The container and attempt directory are removed.
