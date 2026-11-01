# Development progress

Last updated: 2026-11-01

Status is against [`spec/phases.md`](spec/phases.md). A phase is **done** only
when its gate has been proven. "Proven in sim" means the scripted failure exam
([`sim/exam.py`](sim/exam.py)) shows it on the simulated lab: five containers,
each with its own Docker daemon and fake GPU. Real hardware (Windows boot, NVIDIA
drivers, campus network) still has to confirm those gates on a canary PC.

| Status | Meaning |
|--------|---------|
| Done | Gate met in this repo |
| Proven in sim | Gate met on the simulated lab; needs a real-PC run |
| Needs hardware | Code complete; only a real lab PC can prove the gate |

## Summary

| Phase | Spec | Status |
|-------|------|--------|
| 0 | Repo + Postgres/MinIO | **Done** |
| 1 | Control plane core | **Done** (Alembic migrations, idempotent submit) |
| 2 | Scheduler + agent protocol | **Done** |
| 3 | Fake-agent harness | **Done** (race tests in CI; five-PC exam nightly) |
| 4 | Lab inventory | **Needs hardware**: probe the real PCs into `inventory/lab.jsonl` |
| 5 | Real node agent | **Proven in sim**; boot-without-login needs a Windows PC |
| 6 | Fixed-image GPU execution | **Done** (CUDA job on the 3050 PC, 2026-08) |
| 7 | Logs, artifacts, cancel | **Done**; silent jobs now cancel and time out (was broken) |
| 8 | Portal | **Done** (campus OIDC wiring; real issuer needs credentials) |
| 9 | Scheduling policy | **Done** (disk and image-arch matching added) |
| 10 | Environment builder | **Proven in sim** (builder and trainers on separate daemons) |
| 11 | Operations | **Done** in code (alerts, GC, backups, runbooks); runbooks need one canary run |
| 12 | Failure exam + rollout | **Proven in sim**; real five-PC exam and 1 → 60 rollout remain |

## What the simulated exam proves

`make sim && make sim-exam`, 13 scenarios:

- every PC registers; an old agent updates itself to the served version and confirms
- jobs complete with exactly one promoted output each; GPU classes route correctly
- a custom `requirements.txt` builds on the builder, is pushed to the registry,
  and is pulled by digest on a different machine
- agent killed mid-job, PC rebooted mid-job, PC cut off the network: the job is
  retried elsewhere, the stale attempt is fenced, the PC rejoins
- running job cancelled: container stopped and removed
- full disk / busy desktop GPU: the PC takes no work until it recovers
- job over its runtime limit: stopped and marked `timeout`
- duplicate submit with one `Idempotency-Key`: one job

## Added in this round

- Separate `lab_test` database for tests; `make dev` one-command stack
- Alembic migrations (baseline + legacy-database stamping)
- Scheduler: claims lock one job instead of the whole queue; disk and
  image-architecture matching; server-side runtime backstop; stale registry
  digests rebuilt at claim time; `Idempotency-Key` on submit
- Agent: signed self-update (RSA, pinned key), release layout with automatic
  rollback, update rings and minimum version; fake GPU / disk / busy knobs for simulation
- Fix: a job that printed nothing could not be cancelled or timed out
  (blocking `readline`); output is now read on a thread
- Production stack (`deploy/`): Caddy HTTPS, push-protected registry, startup
  refusal on dev defaults, backup/restore scripts (restore verified)
- Operations: alerts (webhook + admin page), storage and image GC, node
  release/revoke, runbooks ([`docs/runbooks.md`](docs/runbooks.md))
- CI: lint, migration check, portal build; nightly simulated exam

## Load test (sim control plane, laptop)

`python -m harness --api http://127.0.0.1:8090 --agents 100 --jobs 500 --seconds 60`
against `sim-control` (one uvicorn worker in Docker on a laptop; 500 jobs queued by 25 users, each fake job
runs 5-15 s with lease renewals and logs; `--chaos` adds dropped leases and stale tokens).

| Measure | Result |
|---|---|
| Jobs completed | 500 of 500 in 61 s (8.2 jobs/s; 100 PCs and ~10 s jobs allow at most 10 jobs/s) |
| Claim latency (500 claims that got a job) | p50 24 ms, p95 734 ms, max 2.2 s |
| Errors | 0 (no 5xx, no refused call, no exception in the server log) |

The slow tail is the first second, when all 100 PCs claim at once and the single worker serves them in turn
(about 7 ms of server time per claim); once the PCs are out of step claims take 10-30 ms. With identical job
lengths the PCs stay in step and each wave of 100 claims queues for up to 1.7 s, so the harness varies job length.

## What's next

The ordered frontend and backend work, with specifications and a progress list, is in [`docs/plan.md`](docs/plan.md).

## What only you can do

1. **Canary lab PC.** Install with `scripts/setup-lab-node.sh` plus the Windows
   boot task; reboot without logging in; confirm it heartbeats, runs a CUDA job,
   self-updates, and rolls back a deliberately broken release.
2. **Inventory.** Run `inventory/probe.py --append inventory/lab.jsonl` on every
   PC and review the quarantine list.
3. **Lab server.** An always-on Linux machine, three DNS names, and IT approval
   for ports 80/443 and a boot-time service on lab PCs (runbook 1).
4. **Campus login.** An OIDC client from university IT (issuer, client id,
   secret, redirect URI `https://<api-host>/auth/callback`).
5. **Real five-PC exam.** Repeat the simulated scenarios by hand: pull power,
   unplug network, fill a disk, Windows update overnight.
6. **Rollout.** 1 → 5 → 15 → 30 → 60 PCs, recording success rate and lease
   losses at each stage.

## Known limits

- Catalog PyTorch image is referenced by tag; pin it with `scripts/mirror-catalog.sh`
  and store the registry digest once the lab registry is up.
- TensorFlow catalog entry is a slim-Python placeholder.
- One control server (no failover); recovery is restart + restore from backup.
- Agent-side image cache is pruned by hand (runbook 7), not automatically.
