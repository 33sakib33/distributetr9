# M11 — Portal

## Goal

Next.js UI after scheduling and execution already work through APIs. Students submit and watch jobs. Admins drain and quarantine nodes.

## In scope

First-release pages:

- Submit job
- Job list
- Job detail with live logs
- Artifact download
- Cancel and retry
- Environment build status (hide or show “catalog only” until M09)
- Admin node list
- Drain and quarantine controls

Timings shown separately: environment building (0 in v1), queue wait, execution, artifact upload.

Queue explanations from `pending_reason`.

## Out of scope

- Designing a new scheduler in the client
- Notebook editor
- Metrics dashboards beyond timings + status

## Dependencies

M05 (jobs actually run), M10 (logs/artifacts). Can stub UI against M02 earlier, but the phase gate is real logs.

## Unlocks

M12 reason copy can be user-facing; M13 admin workflows.

## Contracts

Uses Portal API only. SSE for logs. Auth: OIDC in the browser.

## Behavior

**Submit:** project, archive, entrypoint, args, environment dropdown, resource fields with sane defaults (VRAM 8 GB, runtime 4h).

**List:** status chips, pending reason one-liner, age.

**Detail:** state, node hostname when leased, timings, log panel (stdout/stderr toggle), artifact table, cancel/retry buttons enabled per API.

**Admin:** node table: GPU, class, eligibility, last heartbeat, disk, current job, actions.

Empty and error states: IdP failure, no GPU match (`waiting for a 12 GB GPU`), failed environment (later).

## Failure modes

- SSE disconnect: auto-reconnect with last seq; show “reconnecting”.
- Complete while watching: freeze log, show artifacts.

## Implementation steps

1. App shell + auth.
2. List + detail against real API.
3. Submit form + upload.
4. SSE log viewer.
5. Admin pages gated on `is_admin`.

## Acceptance criteria

- Non-admin cannot see drain/quarantine.
- Live logs append without full page refresh.
- Timings for queue vs run vs upload are visible and sum to roughly wall time.
- Cancel from the UI produces `cancelling` then `cancelled` on a running job.
- Artifact buttons only for promoted files.
