# University Lab ML Job Platform — Modular Spec

This directory is the implementation spec for the product described in [`../features.md`](../features.md). `features.md` is the source of product intent. These files are the build contract.

Read in this order:

1. This index
2. [00-product.md](00-product.md) — users, v1 freeze, deferred work
3. [01-architecture.md](01-architecture.md) — components and runtime layout
4. [02-data-model.md](02-data-model.md) — PostgreSQL and object-storage keys
5. [03-state-machine.md](03-state-machine.md) — job/attempt/build states and races
6. [04-api.md](04-api.md) — Portal API and Agent API
7. [phases.md](phases.md) — step-by-step build order
8. The current module in `modules/`

Do not start a module until its dependencies are done. A module is done only when its acceptance criteria pass.

Living checklist of what is implemented: [`../PROGRESS.md`](../PROGRESS.md).

## Rules

- Scheduler and agent failure protocol before the portal.
- One accepted result per job. One active GPU job per node in v1.
- Every mutating Agent API call is idempotent (safe to repeat after a timeout).
- Large logs and artifacts never go in PostgreSQL.
- Docker executor is the v1 path (Option A: WSL2 + Docker Engine). The non-Docker executor is specified as deferred.

## Module map

| ID | Module | v1? | Depends on |
|----|--------|-----|------------|
| M01 | [Identity and projects](modules/M01-identity-and-projects.md) | Yes | — |
| M02 | [Control plane](modules/M02-control-plane.md) | Yes | M01 |
| M03 | [Lab inventory](modules/M03-lab-inventory.md) | Yes | — |
| M04 | [Scheduler and leases](modules/M04-scheduler-and-leases.md) | Yes | M02 |
| M05 | [Agent protocol](modules/M05-agent-protocol.md) | Yes | M04 |
| M06 | [Fake-agent harness](modules/M06-fake-agent-harness.md) | Yes | M05 |
| M07 | [Node agent](modules/M07-node-agent.md) | Yes | M05, M03 |
| M08 | [Docker executor](modules/M08-docker-executor.md) | Yes | M07 |
| M09 | [Environment builder](modules/M09-environment-builder.md) | Later* | M08 |
| M10 | [Artifacts and logs](modules/M10-artifacts-and-logs.md) | Yes | M02 |
| M11 | [Portal](modules/M11-portal.md) | Yes | M05, M10 |
| M12 | [Scheduling policy](modules/M12-scheduling-policy.md) | Yes | M04, M11 |
| M13 | [Operations](modules/M13-operations.md) | Yes | M07, M11 |
| M14 | [Failure tests and rollout](modules/M14-failure-tests-and-rollout.md) | Yes | All v1 modules |

\*v1 uses administrator-maintained images only. M09 ships after the first real GPU jobs succeed.

```text
M01 Identity ──► M02 Control plane ──► M04 Scheduler ──► M05 Agent protocol
                                              │                    │
M03 Inventory                                 │                    ├──► M06 Fake harness
                                              │                    ├──► M07 Node agent ──► M08 Docker executor ──► M09 Env builder
                                              │                    │
                                              └──► M10 Artifacts ──┴──► M11 Portal ──► M12 Policy
                                                                              │
                                                                              └──► M13 Operations ──► M14 Rollout
```

## Glossary

| Term | Meaning |
|------|---------|
| Fencing token | Secret UUID on one attempt. Stale agents with an old token cannot complete or promote outputs. |
| Lease | Time-bounded assignment of an attempt to a node. Renewed while work runs; expiry starts the unknown/retry path. |
| Attempt | One execution try of a job on one node. Retries create a new attempt and a new output namespace. |
| Promotion | Copy of attempt outputs to the job-level `promoted/` prefix after the scheduler accepts the fencing token. |
| Compatibility class | Inventory label (e.g. `ada-12g`) used for scheduling and image selection. |
| Pull-based agent | Nodes poll the control API for work. The server does not SSH in to start jobs. |
| Idempotent | Repeating the same mutating request after a timeout does not create extra jobs, attempts, or accepted results. |

## Module template

Every module file uses the same sections: Goal, In / out of scope, Dependencies, Unlocks, Contracts, Behavior, Failure modes, Implementation steps, Acceptance criteria.
