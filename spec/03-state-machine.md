# 03 — State machine

Write these transitions down before the portal. Implement them in one scheduler service. Agents report observations. The scheduler decides what is accepted.

## Attempt states

```text
leased → starting → running → uploading → completed
                                      ↘ failed

any active except completed/failed/cancelled
    → cancelling → cancelled

leased | starting | running | uploading | cancelling
    → unknown
    → failed | leased (retry as a new attempt)
```

Active states: `leased`, `starting`, `running`, `uploading`, `cancelling`, `unknown`.

Terminal states: `completed`, `failed`, `cancelled`.

## Job desired state

`run` or `cancel`. Users never patch attempt rows. Cancel sets `desired_state=cancel`. The scheduler then:

- If no attempt: mark job `cancelled` immediately.
- If attempt is active: tell the agent on next renew/heartbeat, transition attempt to `cancelling`, then `cancelled`.
- If a completion is already accepted: leave the job `completed`. Cancel is a no-op. Return 409 with `already_completed`.

## Allowed transitions

| From | To | Who initiates |
|------|----|----------------|
| (new) | leased | scheduler on successful claim |
| leased | starting | agent event |
| starting | running | agent event |
| running | uploading | agent event |
| uploading | completed | agent complete, scheduler accepts |
| leased/starting/running/uploading | failed | agent complete with failure, or scheduler on policy |
| any active | cancelling | scheduler after desired=cancel |
| cancelling | cancelled | agent cleanup or scheduler timeout |
| leased/starting/running/uploading/cancelling | unknown | missed heartbeats past grace |
| unknown | failed | grace exceeded, retry or give up |
| unknown | running | node returns with same boot session and fencing token, lease renewed |
| unknown | cancelled | cancel won while unknown |

Illegal: `completed → anything`, `failed → anything` on the same attempt, `cancelled → anything` on the same attempt. Retries create a **new** attempt.

## Fencing token

Each attempt has one `fencing_token`. Every renew, event, log, complete, and cleanup call must send it.

- Matching token + attempt still accepted: process the call.
- Wrong token: `409 fenced`.
- Token matches a superseded attempt (job already has a different `accepted_attempt_id`, or a newer attempt exists and this one is terminal/unknown-expired): `409 fenced`.
- Duplicate complete with the same token and same outcome: `200` with the original accepted body.

The token is the only way a stale agent is kept from finalizing a job.

## Lease

- TTL is configurable; start at 30s.
- Agent renews at TTL/3.
- Scheduler expires leases with a clock on the server, not the agent.
- On expiry: if disconnect grace has not elapsed, move to `unknown` and keep the attempt. If grace elapses, fail the attempt as `lease_lost` and retry according to policy.

Disconnect grace (start at 60s) is separate from lease TTL so a single missed renew does not kill a long training job.

## Race rules (must be tests)

### Agent disappears

No heartbeat, no renew. After lease expiry → `unknown`. After grace → fail attempt `node_lost` or `lease_lost`. If `max_attempts` remains and `desired_state=run`, enqueue a new attempt. Pending reason: `retrying after node lost`.

### API disappears

Agent keeps the container running and retries renew/complete. It does not start a second job. On reconnect, renew with the same token. If the scheduler already fenced this attempt, agent self-terminates and cleans up.

### Lease expires while the job is healthy

Scheduler: `unknown`, then fail if grace passes. Agent: on next renew, if `409 fenced` or `lease_lost`, kill the cgroup/container, cleanup, stop.

### Cancel and complete at the same time

Serialize on `(job_id)` in the scheduler.

1. If `accepted_attempt_id` is already set to a completed attempt → completion stands.
2. Else if `desired_state=cancel` is committed first → reject complete with `409 cancelled`; attempt goes `cancelling` then `cancelled`. Partial uploads stay in the attempt namespace and are not promoted.
3. Else completion with a valid token and exit 0 → accept, promote artifacts, ignore subsequent cancel.

### Completion reported twice

Same token, same exit: 200 idempotent. Same token, different exit: 409 conflict, keep the first accepted body, alert. Different token: 409 fenced.

### Completion response lost

Agent retries complete. Scheduler returns the original acceptance. Agent then cleanup-complete.

### Node restarts with a leftover process

New `boot_session_id` on register. Scheduler fences any attempt bound to the old session (fail `node_lost` if not already terminal). Agent reconcile: list leftover containers/directories, kill and delete them, report cleanup. Never resume a container across boot sessions in v1.

### Two claim requests from the same node

Idempotent claim: if the node already has an active attempt, return that claim. Otherwise at most one new claim. Database unique constraint on active attempt per node is the backstop.

### Stale attempt cannot finalize

Once a newer attempt is leased, or the job is accepted, old tokens cannot complete, upload-promote, or change desired state.

## Retry policy

- New attempt only from terminal `failed` (and not user_code if policy says do not retry user errors).
- v1 retries: `lease_lost`, `node_lost`, `internal`. Does not retry `user_code`, `cancelled`, `timeout` (timeout is a user-visible failure).
- `attempt_number` increments. `max_attempts` on the job.
- Each attempt has its own `output_namespace`.

## Environment builds (later)

`pending → building → testing → ready` or `failed`. A job is not `queued` for GPU claim until its environment is `ready`. v1 skips this path and points at catalog `environments.image_digest` directly.

## Implementation constraint

All of the above lives in the scheduler module. Agents and the portal are clients. They do not invent extra states.
