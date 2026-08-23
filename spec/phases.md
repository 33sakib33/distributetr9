# Implementation phases

Build in this order. Do not skip to the portal. Each phase ends with a concrete demo or test gate.

```text
1  State machine + database          M01, M02, data model, 03
2  Fake agents + concurrency tests   M04, M05, M06
3  One real WSL agent                M03, M07
4  Fixed-environment GPU execution   M08 (catalog images only)
5  Lease renewal + cancellation      M04/M05/M07 hardening
6  Logs and artifacts                M10
7  Docker executor limits            M08 complete
8  Environment builder               M09 (after GPU jobs work)
9  Portal                            M11
10 Fair scheduling and quotas        M12
11 Operations tooling                M13
12 Failure tests + gradual rollout   M14
```

## Phase 0 — Repo and freeze

**Modules:** [00-product.md](00-product.md)

1. Keep the v1 freeze in 00-product as the scope checklist.
2. Create the monorepo layout: `control/`, `agent/`, `supervisor/`, `portal/`, `harness/`, `spec/`.
3. Compose file for PostgreSQL + MinIO only. Registry can wait until M08.

**Gate:** `docker compose up` gives Postgres and MinIO. No feature work outside v1.

## Phase 1 — Control plane core

**Modules:** [M01](modules/M01-identity-and-projects.md), [M02](modules/M02-control-plane.md)

1. Identity (OIDC + dev header).
2. Projects and membership.
3. Migrations for every table in the data model (including unused `environment_builds`).
4. Job create/get/cancel/retry without execution (jobs stay `queued`).
5. Artifact upload of the submission archive to MinIO.

**Gate:** curl can create a project, upload a script archive, and fetch the job as `queued`.

## Phase 2 — Scheduler protocol without GPUs

**Modules:** [M04](modules/M04-scheduler-and-leases.md), [M05](modules/M05-agent-protocol.md)

1. Implement the state machine in one service.
2. Agent register, heartbeat, claim, renew, events, complete, cleanup.
3. Fencing and lease expiry workers.

**Gate:** a single scripted agent can claim, renew, complete; a second complete with a bad token is rejected.

## Phase 3 — Fake-agent harness

**Module:** [M06](modules/M06-fake-agent-harness.md)

1. Simulate ~100 agents with mixed GPU classes.
2. Concurrent claims, random disconnects, double complete, lost responses, cancel-during-complete.

**Gate:** CI proves: one accepted result per job, one active GPU job per node, stale attempts cannot finalize, cancel is eventually enforced, expired jobs retry per policy.

Do not provision lab PCs for scheduling until this gate is green.

## Phase 4 — Lab inventory

**Module:** [M03](modules/M03-lab-inventory.md)

1. Inventory tool records GPU, VRAM, CC, Windows, driver, CPU, RAM, disks, WSL, PyTorch CUDA test.
2. Assign compatibility classes. Quarantine unsupported GPUs.

**Gate:** a spreadsheet or `nodes` seed file covering the lab, with quarantine list. Scheduler development on real hardware uses only eligible classes.

## Phase 5 — Real node agent, no training yet

**Module:** [M07](modules/M07-node-agent.md)

1. Windows service account + supervisor (start at boot, restart WSL agent).
2. WSL agent registers, heartbeats, long-polls, reconciles leftovers.
3. CUDA health check. Report `gpu_desktop_busy`.

**Gate:** one lab PC appears in `GET /nodes`, survives reboot, and claims a no-op job that exits 0 without GPU training.

## Phase 6 — Fixed-image GPU execution

**Module:** [M08](modules/M08-docker-executor.md) (catalog images)

1. Maintained images: PyTorch stable+CUDA, TensorFlow stable, PyTorch+tools, optional compiler image.
2. Pull by digest. Read-only input. Attempt-scoped writable dirs.
3. CPU, RAM, PID, runtime limits. Graceful then forced kill. Remove container.

**Gate:** `train.py` using CUDA runs on one PC, exit 0, outputs in MinIO.

## Phase 7 — Logs, artifacts, cancel on a real node

**Modules:** [M10](modules/M10-artifacts-and-logs.md), cancel path in M04/M07

1. Sequenced compressed log chunks, rate limits.
2. Output size limits and checksums.
3. Promote only after fencing verification.
4. Cancel during running and during upload.

**Gate:** portal-API (still curl/SSE) streams logs live; cancel stops the container; node disk can be wiped without losing promoted artifacts.

## Phase 8 — Portal

**Module:** [M11](modules/M11-portal.md)

Pages: submit, list, detail+live logs, artifacts, cancel/retry, admin nodes, drain/quarantine. Show queued / running / uploading times separately.

**Gate:** a non-author user can submit and watch a job without using curl.

## Phase 9 — Scheduling policy

**Module:** [M12](modules/M12-scheduling-policy.md)

Match GPU class, VRAM, CC, CPU, RAM, env compatibility, health, disk pressure. Add user/project concurrency, queue aging, max runtime, retry limits, high-VRAM reservation, pending reasons.

**Gate:** a 24 GB job does not occupy a 12 GB card; a 12 GB job can still run; pending_reason is visible.

## Phase 10 — Environment builder (optional vs v1)

**Module:** [M09](modules/M09-environment-builder.md)

Only after Phases 6–8 work. Custom deps, hash, cache by digest, CUDA preflight, build logs separate from training logs.

**Gate:** two jobs with the same lockfile reuse one image; a broken spec fails at build, not on a GPU node.

## Phase 11 — Operations

**Module:** [M13](modules/M13-operations.md)

Drain, replace WSL, update agent/drivers/WSL, cache GC, VHD compact, credential rotation, quarantine, restore Postgres+MinIO. Canary group. Alerts: offline, disk, lease loss, repeated failures, queue age.

**Gate:** written runbooks exist and were executed once on a canary PC.

## Phase 12 — Five-machine failure exam, then rollout

**Module:** [M14](modules/M14-failure-tests-and-rollout.md)

Run the listed failure tests. Do not expand the fleet until every outcome is deterministic and visible. Then 1 → 5 → 15 → 30 → 60 PCs with the listed metrics.

**Gate:** success rate and lease-loss numbers are recorded at each stage.
