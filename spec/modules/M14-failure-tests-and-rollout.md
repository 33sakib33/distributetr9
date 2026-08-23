# M14 — Failure tests and rollout

## Goal

Do not grow the fleet until failure behavior is deterministic and visible in the portal. Then roll out 1 → 5 → 15 → 30 → 60 PCs, measuring the listed metrics at each stage.

## In scope

Deliberate tests on five machines covering different GPU classes:

- Kill the agent during training
- Kill WSL during training
- Reboot Windows
- Disconnect Tailscale (or lab overlay)
- Stop the scheduler
- Lose an assignment (claim) or completion response
- Fill the Linux VHD
- Fill the Windows volume
- Cancel during startup
- Cancel during artifact upload
- Two claim requests from the same node
- Windows GPU application during training
- Submit a broken environment (v1: broken script; M09: broken deps)
- Output exceeding quota

Rollout stages and metrics.

## Out of scope

- Chaos in production without the five-machine exam
- Distributed training tests

## Dependencies

All other v1 modules.

## Unlocks

Production lab use.

## Contracts

Test log template (one row per test):

| field | |
|-------|--|
| test name | |
| setup | |
| action | |
| expected portal status | |
| expected artifacts | |
| expected node state | |
| observed | |
| pass | yes/no |

Rollout metrics (export from DB):

- Job success rate
- Environment build time (0 in v1)
- Queue time
- Lease-loss frequency
- Node offline frequency
- Cold-cache download load (registry bytes)
- Disk growth (VHD and MinIO)
- Cancellation latency (cancel request → terminal)
- Checkpoint recovery success (n/a in v1)
- Failures by GPU and driver class

Stages: 1 dev PC → 5 PCs mixed GPU class → 15 volunteer users → 30 PCs → all 60.

## Expected outcomes (normative)

| Test | Expected |
|------|----------|
| Kill agent | Supervisor restarts agent; same boot session resumes renew **or** session rotates and job retries; never two accepted results |
| Kill WSL | Boot session rotates; attempt `node_lost` / retry; leftovers gone after reconcile |
| Reboot Windows | Same as WSL kill; supervisor starts without login |
| Disconnect overlay | Attempt `unknown`, then resume or retry per grace; portal shows `unknown` then a terminal or running state |
| Stop scheduler | Agent keeps container, retries API; on return, renew or fence+kill |
| Lost claim response | Idempotent claim returns the same attempt; one container |
| Lost complete response | Retry complete; one promotion |
| Fill Linux VHD | No new claim; alert; running job may fail `resource` visibly |
| Fill Windows volume | Supervisor heartbeat; no claim; alert |
| Cancel during startup | `cancelled`, container removed, nothing promoted |
| Cancel during upload | `cancelled` or `completed` per race rules; never partial promote |
| Double claim | One active attempt |
| Windows GPU app | `gpu_desktop_busy`; no new jobs; running job may slow (acceptable) or fail visibly |
| Broken script | `failed` `user_code`, logs available |
| Output over quota | `failed` `resource`, no promote |

## Implementation steps

1. Five-machine lab with mixed classes from M03.
2. Run the table; fix scheduler/agent until every row is deterministic.
3. Record baseline metrics on 5 PCs.
4. Open to volunteers at 15 only after the exam passes.
5. Canary updates (M13) during 30 and 60.

## Acceptance criteria

- Every test row is `pass` with a portal screenshot or API trace attached.
- No unexplained `unknown` that lasts past grace without becoming failed/retrying.
- Stage N+1 starts only after stage N metrics are written to `ops/rollout-log.md`.
- 60-PC stage has alerts wired (M13).
