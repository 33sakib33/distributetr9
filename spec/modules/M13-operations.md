# M13 — Operations

## Goal

Administrators can drain, update, clean, rotate credentials, quarantine, restore, and receive alerts. Updates go through a canary group.

## In scope

Runbooks + the API/tooling to execute them:

- Drain a node
- Replace its WSL distro
- Update the agent
- Update NVIDIA drivers
- Update WSL
- Clean image/environment caches
- Compact the WSL VHD
- Rotate node credentials
- Quarantine failing hardware
- Restore control database and object store
- Image cache quotas and GC
- Node disk high-watermark protection
- Staged Windows / WSL / driver updates
- Alerts: offline nodes, disk pressure, lease loss, repeated failures, queue age
- Inventory of Windows, WSL, driver, agent, image versions (from heartbeats)

## Out of scope

- Full cluster autoscaling
- Student self-serve node admin

## Dependencies

M07, M11 admin pages. Alerts can be email/Slack webhook.

## Unlocks

M14 fleet expansion.

## Contracts

Admin API already has drain/quarantine. Add:

- `POST /nodes/{id}/rotate-token` → new token shown once; agent config must be updated (supervisor pull or USB/lab script)
- `GET /admin/inventory` → versions
- `GET /admin/alerts` or outbound webhooks

Watermarks (defaults): Linux VHD free < 20 GB → no claim, heartbeat flag, alert. Windows volume < 15 GB → same.

GC: unused images not referenced by catalog/builds and older than 14 days; cap cache bytes per node (e.g. 80 GB).

Canary: node label `update_group=canary|stable`. Procedures run on canary first.

## Behavior

**Drain:** eligibility=draining, finish current attempt, no claims. When idle, admin proceeds with WSL replace/driver update.

**Replace WSL:** drain, export nothing of student data (attempts are disposable), import golden distro, copy agent config, boot, confirm heartbeat + CUDA test.

**Restore:** Postgres dump + MinIO erasure-coded disks. Document RPO; v1 nightly dump is enough. Jobs in flight during restore may need cancel.

## Failure modes

- Update without drain leaves a half-upgraded GPU driver mid-training → always drain.
- Token rotation while agent running: agent 401s, supervisor should read new token from a Windows-protected file dropped by the update script.

## Implementation steps

1. Write runbooks in `ops/` (one markdown per procedure).
2. Watermark + GC in agent.
3. Alert worker on control plane (queue age > 30m, node offline > 10m, lease_lost count, disk).
4. Canary group field on nodes.
5. Backup scripts.

## Acceptance criteria

- Each procedure has been executed once on a canary PC.
- Drain prevents new claims within one heartbeat interval.
- Disk below watermark: no new claims, alert fired, current job can finish.
- Restore of Postgres+MinIO on a staging compose brings jobs and artifacts back.
- Version inventory lists driver, WSL, agent, image digest per node.
