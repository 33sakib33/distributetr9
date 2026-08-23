# M05 — Agent protocol

## Goal

HTTP surface agents use to register, poll for work, renew leases, stream logs, and complete. Pull-based. Long-polling. Idempotent mutations.

## In scope

All Agent API routes in [04-api.md](../04-api.md).

Node token issue/hash/verify. Boot session header. Heartbeat persistence. Command channel (`drain`, `cancel_attempt`, `self_terminate`).

## Out of scope

Windows/WSL implementation (M07). Fake agents (M06) consume this API.

## Dependencies

M04.

## Unlocks

M06, M07.

## Contracts

Route table as in 04-api. Middleware:

1. Resolve node from bearer token hash.
2. Require `X-Boot-Session-Id` matches `nodes.boot_session_id` except on `register`.
3. On mismatch: 409 `stale_boot_session`; agent must register.

Long-poll `POST /agent/claim`: wait up to 25s or until a matching job exists. If the node is draining/quarantined/offline, return immediately with `attempt: null`.

## Behavior

- Register without `node_id`: create node, return token once. Store only hash.
- Register with `node_id`: rotate `boot_session_id`, update inventory columns, call `on_node_boot`.
- Heartbeat updates `last_heartbeat_at`, disk, `gpu_desktop_busy`. If last heartbeat older than 3× interval, eligibility can show `offline` until a heartbeat arrives (do not auto-quarantine).
- Self-termination command: scheduler lost the lease and wants the agent to kill work even if the agent still thinks it holds it.

## Failure modes

- Token leak: rotation in M13; v1 tokens long-lived hashed secrets.
- Claim timeout vs proxy: document that lab proxies must allow 30s POST.

## Implementation steps

1. Token hashing (argon2 or bcrypt) and register.
2. Heartbeat.
3. Claim long-poll wired to `scheduler.claim`.
4. Acknowledge, renew, events, logs (logs body stored in M10; if M10 stub, persist seq in DB only).
5. Complete and cleanup-complete.
6. Contract tests: replay each mutating call.

## Acceptance criteria

- Repeating claim, renew, complete after a timeout does not duplicate attempts or accepted results.
- Stale fencing token is 409 on events/complete.
- Long-poll returns a job within 1s of enqueue when a matching idle node is waiting.
- New boot session fences the previous attempt.
