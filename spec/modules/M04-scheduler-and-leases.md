# M04 — Scheduler and leases

## Goal

The only component that accepts job results, grants claims, expires leases, and applies retries. Implements [03-state-machine.md](../03-state-machine.md).

## In scope

- Claim matching (v1 simple: any eligible node with enough VRAM)
- Attempt creation with fencing token and lease
- Lease renew and expiry worker
- Disconnect grace → `unknown` → fail → retry
- Cancel propagation
- Accept completion with fencing
- Artifact promotion trigger (call into M10)
- Derived job status and `pending_reason`

## Out of scope

- Fairness, aging, high-VRAM reservation (M12; keep function stubs)
- Environment build queue (M09)
- Portal (M11)

## Dependencies

M02. Agent HTTP handlers may live in M05 but must call this module.

## Unlocks

M05, M06.

## Contracts

Python module `scheduler/` used by FastAPI routes. All writes that change attempt or job acceptance run in a single transaction with `SELECT … FOR UPDATE` on the job row.

Public functions (names indicative):

- `claim(node) -> Attempt | None`
- `renew(attempt_id, token, observed_state) -> Lease`
- `on_event(attempt_id, token, state)`
- `complete(attempt_id, token, payload) -> AcceptedResult`
- `request_cancel(job_id)`
- `expire_leases(now)`
- `on_node_boot(node_id, new_boot_session_id)`

`pending_reason` examples:

- `waiting for a 12 GB GPU`
- `waiting for node class ada-12g`
- `waiting for user concurrency slot`
- `node lab-12 starting`
- `retrying after node lost`
- `waiting for disk on eligible nodes`

v1 may only emit the GPU/class/concurrency reasons that M12 later expands.

## Behavior

**Claim**

1. Skip nodes not `eligible`, heartbeat stale, `gpu_desktop_busy`, disk below watermark.
2. Skip if node already has an active attempt (return existing).
3. Pick oldest queued job whose resources fit this node (v1: FIFO among matching).
4. Insert attempt `leased`, unique fencing token, `lease_expires_at=now+ttl`.
5. Enforce one active GPU job per node via partial unique index; on conflict, retry the transaction.

**Expiry worker** (every 2s):

- `now > lease_expires_at` and state active and not already `unknown` → `unknown`.
- `unknown` for longer than grace → fail attempt `lease_lost`, maybe enqueue retry.

**Complete**

Apply race rules in 03. Promote outputs only after accept.

**Boot**

Any non-terminal attempt for this node with a different `boot_session_id` fails `node_lost`.

## Failure modes

See 03. Clock: use database `now()` so API replicas cannot split-brain on expiry. v1 is a single API process; still use DB time.

## Implementation steps

1. Attempt insert + claim query.
2. Renew + fencing checks.
3. Expiry worker.
4. Complete + cancel serialization.
5. Retry policy.
6. Tests in M06 are the real bar; add focused unit tests per race listed in 03.

## Acceptance criteria

- Two concurrent claims on one node never create two active attempts.
- Double complete: one accepted body, second 200 or 409 per 03.
- Wrong token never sets `accepted_attempt_id`.
- Lease expiry produces retry when policy allows.
- Cancel of a leased job reaches `cancelled` without a later complete promoting artifacts.
