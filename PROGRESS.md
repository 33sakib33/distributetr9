# Development progress

Last updated: 2026-08-19

Status is against [`spec/phases.md`](spec/phases.md) and the module specs. A phase is **done** only when its gate has been proven, not merely sketched in code.

| Status | Meaning |
|--------|---------|
| Done | Gate met in this repo |
| Partial | Real code exists, gate not fully met |
| Not started | No implementation yet |

## Summary

| Phase | Spec | Status |
|-------|------|--------|
| 0 | Repo + Postgres/MinIO | **Partial** |
| 1 | Control plane core | **Partial** |
| 2 | Scheduler + agent protocol | **Partial** |
| 3 | Fake-agent harness | **Partial** |
| 4 | Lab inventory | **Partial** |
| 5 | Real node agent | **Partial** |
| 6 | Fixed-image GPU / Docker executor | **Partial** |
| 7 | Logs, artifacts, cancel on a real node | **Partial** |
| 8 | Portal | **Partial** (OIDC + optional dev header) |
| 9 | Scheduling policy | **Partial** |
| 10 | Environment builder | **Partial** |
| 11 | Operations | Not started |
| 12 | Failure tests + rollout | Not started |

**Proven so far:** submit a Python archive on this Mac, an agent on `DESKTOP-N7057V6` claims it, a CUDA `train.py` runs in the PyTorch image, logs stream, outputs land in MinIO. Portal is a Next.js UI over the same API (dev header auth).

---

## Phase 0 — Repo and freeze

- [x] v1 freeze written in [`spec/00-product.md`](spec/00-product.md)
- [x] Modular spec (`spec/` + modules)
- [x] `control/` FastAPI app
- [x] `agent/` stdlib agent
- [x] `docker-compose.yml` for Postgres + MinIO
- [x] Gate: Postgres and MinIO run
- [x] `harness/` package
- [x] `supervisor/` package (scripts; not yet proven on a lab PC)
- [x] `portal/` package layout
- [x] Registry service (`registry:2` in compose); custom builds require `REGISTRY_URL` and a builder node

## Phase 1 — Control plane core (M01, M02)

- [x] Dev auth (`X-Dev-User` when `DEV_AUTH=1`)
- [x] Users, projects, membership
- [x] Schema for jobs, attempts, nodes, environments, artifacts, builds
- [x] `POST /jobs` with archive inspect + MinIO store
- [x] `GET /jobs`, `GET /jobs/{id}`, cancel, retry
- [x] Catalog environments seeded (PyTorch image `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`)
- [x] Gate: smoke job creates as `queued` ([`scripts/smoke.py`](scripts/smoke.py))
- [x] University OIDC (local mock IdP; real issuer via `OIDC_*`)
- [ ] Alembic migrations (`create_all` on startup instead)

## Phase 2 — Scheduler protocol (M04, M05)

- [x] Attempt state machine in [`control/app/scheduler.py`](control/app/scheduler.py)
- [x] Register, heartbeat, claim (long-poll), acknowledge, renew, events, complete, cleanup
- [x] Fencing token checks
- [x] Lease expiry worker (`unknown` → `lease_lost` / retry)
- [x] Gate (happy path): local agent claimed, renewed, completed one job
- [x] Automated test: second complete with a bad token is rejected
- [x] Concurrent claim: one active attempt per node
- [x] Complete is idempotent; different exit code is 409
- [ ] Idempotency-Key store

## Phase 3 — Fake-agent harness (M06)

- [x] Harness client + `python -m harness --agents 100 --jobs 500 --seconds 60`
- [x] Targeted race tests in [`tests/test_scheduler_races.py`](tests/test_scheduler_races.py)
- [x] Gate: one accepted result, one job per node, stale token rejected, cancel vs complete, lease expiry retry, boot-session fence, complete idempotent
- [x] CI job running these tests on every push ([`.github/workflows/tests.yml`](.github/workflows/tests.yml))
- [ ] 60s / 100-agent stress as a required CI check

Run: `docker compose up -d postgres minio && control/.venv/bin/pytest tests -v`

## Phase 4 — Lab inventory (M03)

- [x] Inventory probe ([`inventory/probe.py`](inventory/probe.py)) — GPU, VRAM, CC, driver, disks, WSL, optional CUDA smoke
- [x] Compatibility class catalog ([`inventory/classes.json`](inventory/classes.json)) including `ampere-8g` / `ada-8g`
- [ ] JSONL covering the real lab PCs (`inventory/lab.jsonl`)
- [ ] Human-reviewed quarantine list before those hosts claim
- [x] Agent registers the same class names (no more generic `gpu`)

## Phase 5 — Node agent (M07)

- [x] Pull-based Python agent ([`agent/agent.py`](agent/agent.py))
- [x] Register, heartbeat, claim, local `python` or Docker executor, logs, output upload
- [x] GPU class + `nvidia-smi`; optional `--cuda-preflight`
- [x] Reconcile leftover `lab-*` containers after restart (Docker executor)
- [x] `gpu_desktop_busy` heuristic; skip claim when unhealthy / drain / quarantine
- [x] `--config` JSON (Windows-held secrets copied into WSL)
- [x] Supervisor scripts ([`supervisor/`](supervisor/)) — scheduled task / WinSW; **not yet run on a lab PC**
- [ ] Managed WSL distro imported as `lab-ml`
- [ ] Gate: desktop on the same Wi-Fi stays registered after reboot with no login

## Phase 6–7 — Docker executor + logs (M08, M10)

- [x] Sequenced log chunks in MinIO
- [x] Output checksum + promote after fencing
- [x] Artifact download API
- [x] Cancel API (queued / in-progress)
- [x] SSE live log stream (`GET /jobs/{id}/logs/stream`, `Last-Event-ID`)
- [x] One container per attempt, cgroup limits, `--read-only`, `--network none`, no docker.sock
- [x] Graceful then force kill; always `docker rm`; reconcile leftover `lab-*`
- [x] `--gpus` only when `nvidia-smi` works and the job asks for VRAM (skipped on this Mac)
- [x] Log rate / size limits (drop extras, one warning line; no 429)
- [x] Output file / attempt size limits; oversized complete is `resource` and is not promoted
- [x] Catalog PyTorch image `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime` (tag; pin `@sha256` next)
- [x] Gate: CUDA `train.py` on a lab PC, outputs in MinIO

## Phase 8 — Portal (M11)

- [x] Next.js submit / list / detail
- [x] Live logs
- [x] Artifact download UI
- [x] Cancel / retry
- [x] Admin node list, drain, quarantine

## Phase 9 — Scheduling policy (M12)

- [x] FIFO claim among jobs that fit the node
- [x] User / project concurrency quotas (defaults)
- [x] Pending reasons (`waiting for node class`, VRAM, disk, reservation)
- [x] High-VRAM reservation (24 GB nodes skip small jobs until aging steal)
- [x] Aging steal when no large job is waiting
- [ ] Match on compute capability / environment image vs node class (partial: CC is checked)

## Phase 10 — Environment builder (M09)

- [x] User `requirements.txt` on job create (hash of deps + base image + GPU class)
- [x] Reuse the same image when two jobs share that hash
- [x] Lab agent Docker build + CUDA/import test; job stays `building` until ready
- [x] Ready requires a registry `name@sha256:…` (local docker ids are failed builds)
- [x] Train nodes never claim builds; `--role build` never claims training jobs
- [x] Failed build → job `failed`; pip output in `/jobs/{id}/build-logs`, not training logs
- [x] Catalog environments still work with no build row
- [ ] Dedicated builder node on a lab PC; pin catalog image to a registry RepoDigest after `scripts/mirror-catalog.sh`

## Phase 11 — Operations (M13)

- [x] Drain / quarantine HTTP endpoints
- [ ] Runbooks, canary updates, credential rotation
- [ ] Disk watermark alerts, image GC, backups

## Phase 12 — Failure tests + rollout (M14)

- [ ] Five-machine failure exam (kill agent, WSL, reboot, fill disk, …)
- [ ] Staged rollout 1 → 5 → 15 → 30 → 60 PCs

---

## Module checklist

| ID | Module | Status |
|----|--------|--------|
| M01 | Identity and projects | Partial (OIDC + dev header; no Alembic) |
| M02 | Control plane | Partial (create_all, not Alembic) |
| M03 | Lab inventory | Partial (probe + classes; no lab JSONL yet) |
| M04 | Scheduler and leases | Partial (races tested in CI) |
| M05 | Agent protocol | Partial (happy path + races tested) |
| M06 | Fake-agent harness | Partial (targeted tests pass; no CI 100-agent run) |
| M07 | Node agent | Partial (supervisor scripts ready; boot-without-login unproven) |
| M08 | Docker executor | Partial (CUDA job proven on 3050; tag not digest-pinned yet) |
| M09 | Environment builder | Partial (registry digest required; builder vs train roles; no dedicated lab builder yet) |
| M10 | Artifacts and logs | Partial (SSE + limits; orphan GC later) |
| M11 | Portal | Partial (Next.js + OIDC session; mock IdP until campus app) |
| M12 | Scheduling policy | Partial (FIFO, quotas, reservation, steal, explain) |
| M13 | Operations | Not started |
| M14 | Failure tests and rollout | Not started |

## Next to build

1. Copy `agent/` to the lab PC (`0.5.0`) so it can claim environment builds.
2. Pin `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime` to an immutable `@sha256:…` digest from the lab PC (`docker inspect`).
3. Windows boot task (`C:\\lab\\supervisor\\`) so the agent heartbeats with no login.
4. Google sign-in limited to campus email domains.
5. Inventory JSONL for real lab PCs.
