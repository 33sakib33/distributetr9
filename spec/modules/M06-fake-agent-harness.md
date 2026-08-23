# M06 — Fake-agent harness

## Goal

Prove the scheduler protocol with ~100 simulated agents before any lab PC runs training. This is the quality gate for M04/M05.

## In scope

- Simulator process(es) that speak the real Agent API
- Mixed GPU types and VRAM
- Concurrent claims
- Lease renewals
- Random disconnects
- Double completion
- Dropped HTTP responses (retry)
- Cancel while completing
- Pytest (or equivalent) assertions listed below

## Out of scope

- Real Docker, GPU, Windows
- Portal

## Dependencies

M05.

## Unlocks

Real agent work (M07). Do not skip this module.

## Contracts

Harness command: `python -m harness --agents 100 --jobs 500 --seconds 60`.

Each fake agent loop:

1. Register with a GPU class from a weighted table.
2. Heartbeat + long-poll claim.
3. Sleep a random “runtime”.
4. Renew on interval.
5. With probability p: drop complete response and retry; complete twice; disconnect; ignore cancel for a while; send old fencing token.

Control API under test is the real app + Postgres. MinIO can be fake or real.

## Behavior

Chaos knobs (defaults):

| knob | default |
|------|---------|
| disconnect_prob per renew | 0.02 |
| double_complete_prob | 0.05 |
| lose_response_prob | 0.05 |
| cancel_race_prob | 0.05 |
| stale_token_prob | 0.02 |

## Failure modes

Harness bugs vs scheduler bugs: if an invariant fails, fail CI. Do not retry-flakes away.

## Implementation steps

1. HTTP client matching Agent API, including retries on 5xx and timeouts.
2. In-process or dockerized API.
3. Invariant checkers querying Postgres after the run (and continuously).
4. CI job, 60s stress + targeted race tests for each bullet in 03.

## Acceptance criteria (must prove)

1. A job has at most one `accepted_attempt_id` pointing at a completed or failed terminal result (failed with retries may be null while queued; never two completed promotions).
2. A node has at most one active GPU attempt at any snapshot.
3. Stale attempts cannot set `accepted_attempt_id` or write `promoted/` keys.
4. Cancellation is eventually enforced: every cancel request ends in `cancelled` or `completed` (if complete won the race), never stuck `cancelling` beyond a timeout (e.g. 2 minutes in sim).
5. Expired leases retry until `max_attempts`, then `failed` with `lease_lost`.

## Tests to keep forever

- `test_one_accepted_result`
- `test_one_job_per_node`
- `test_stale_token_rejected`
- `test_cancel_vs_complete`
- `test_lease_expiry_retry`
- `test_boot_session_fences_old_attempt`
- `test_complete_idempotent`
