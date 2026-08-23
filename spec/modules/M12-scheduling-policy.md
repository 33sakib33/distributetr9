# M12 — Scheduling policy

## Goal

Match jobs to nodes fairly enough for a 60-PC lab, with clear pending reasons. Built after a dumb FIFO matcher already runs jobs.

## In scope

Matching on:

- GPU model or compatibility class
- Minimum allocatable VRAM
- Compute capability
- CPU and RAM
- Environment compatibility (catalog image vs node class)
- Node health and disk pressure

Then:

- Per-user concurrency
- Per-project concurrency
- Queue aging
- Maximum job runtime (already on the job; enforce at claim and in executor)
- Retry limits (already in M04; expose in UI)
- Reserve high-VRAM GPUs for jobs that need them
- Clear pending reasons in the portal

## Out of scope

- Preemption of running jobs (optional later; drain is the admin tool)
- Paid priority

## Dependencies

M04, M11 (to display reasons). Matching can land in M04 behind flags.

## Unlocks

Multi-user lab use without 24 GB cards stuck on tiny jobs.

## Contracts

`scheduler.match_score(job, node) -> None | int` (higher is better) plus `explain(job) -> pending_reason`.

High-VRAM reservation: nodes with `vram_allocatable_mb >= HIGH_VRAM_MB` (default 20480) only match jobs with `min_vram_mb >= HIGH_VRAM_RESERVE_MIN` (default 16384) **unless** no other job has been waiting for that node class longer than `aging_steal_seconds` (default 15 minutes). Document the numbers in config.

Concurrency: count attempts in active states plus queued jobs? Count **running+starting+leased+uploading** per user/project. Queued jobs wait with reason `waiting for user concurrency slot`.

Aging: FIFO by `created_at` among jobs that fit, plus a boost after `age_boost_seconds` so they outrank newer smaller jobs when reservation would otherwise starve them.

## Behavior

Explain algorithm (first matching failure wins):

1. No eligible nodes of required class → `waiting for node class {class}`
2. No node with enough VRAM → `waiting for a {n} GB GPU`
3. All matching nodes disk-pressured → `waiting for disk on eligible nodes`
4. User at quota → `waiting for user concurrency slot`
5. Project at quota → `waiting for project concurrency slot`
6. High-VRAM reserved → `waiting for a smaller GPU or high-VRAM reservation`
7. Else `queued`

## Failure modes

- Starvation of small jobs: aging steal.
- Starvation of large jobs: reservation.
- Mis-set class on a node: job never matches; inventory fix + quarantine.

## Implementation steps

1. Replace FIFO claim query with filtered match.
2. Quota checks.
3. Reservation + aging.
4. `explain()` on GET job.
5. Tests with a fixture of 12 GB and 24 GB fake nodes.

## Acceptance criteria

- An 8 GB job does not claim a reserved 24 GB node while a 12 GB node is idle.
- An 8 GB job *can* claim the 24 GB node after aging steal if no large job exists.
- User with quota 1 never has two active attempts.
- Portal shows the same reason string as `jobs.pending_reason`.
