# M07 — Node agent (Windows supervisor + WSL agent)

## Goal

Every lab PC runs a supervisor as a Windows service and an agent inside a managed WSL distro. The agent speaks M05, reconciles leftovers after reboot, and never exposes credentials to jobs.

## In scope

- Dedicated Windows service account
- Supervisor: start at boot without interactive login, launch agent as a foreground WSL process, restart on failure, host health (GPU desktop busy, Windows disk, reboot status)
- WSL agent: register, CUDA health check, long-poll, stage submission, start executor (interface), renew, logs, cancel, upload, complete with fencing token, clean attempt dir, reconcile
- First execution target: one administrator-maintained test environment (can be `python:noop` then catalog PyTorch)

## Out of scope

- Docker details (M08) beyond an `Executor` interface
- Image builds (M09)
- Driver/WSL update orchestration (M13)

## Dependencies

M05, M03 (class names). Can develop against the fake API on a single PC.

## Unlocks

M08.

## Contracts

**Supervisor → WSL**

```text
wsl -d lab-ml --cd /opt/lab-agent -- ./agent run --config /etc/lab-agent/config.json
```

Config (Windows-held secrets, copied into a root-owned WSL file mode 0400):

```json
{
  "api_base": "https://control.lab.example",
  "node_id": "uuid-or-null",
  "node_token": "...",
  "compatibility_class": "ada-12g"
}
```

**Executor interface** (implemented in M08):

```text
stage(attempt) -> paths
start(attempt) -> handle
signal_graceful(handle)
kill(handle)
collect_logs(handle) -> iterator
wait(handle) -> exit_code
remove(handle)
reconcile() -> leftover handles
```

**Self-termination:** if renew returns fenced or command is `self_terminate`, kill work, cleanup, do not claim until next heartbeat says `continue`.

## Behavior

**Supervisor**

1. Service recovery: restart on crash, delayed restart on repeated failure.
2. If WSL is stopped, `wsl --terminate` then start again.
3. Detect GPU desktop use: nvidia-smi process list intersecting known interactive apps, or GPU utilization while no agent job. Set `gpu_desktop_busy`.
4. Report Windows disk and last boot time.

**Agent**

1. On start: CUDA preflight (small tensor). If fail, heartbeat `agent_healthy=false` and do not claim.
2. Reconcile leftovers before claim.
3. One claim at a time.
4. Stage input under `/var/lab/attempts/{attempt_id}/`.
5. Renew loop independent of training process.
6. On cancel desired_state: graceful signal, wait grace (default 15s), force kill, still try to upload partial logs, complete as cancelled.
7. After complete response (or fenced): delete attempt dir and container.
8. Never mount `node_token`, Docker socket, or `/var/lab/agent` into the job.

## Failure modes

- User logs in and plays a game: `gpu_desktop_busy=true`, no new claims; current job keeps running unless admin drains.
- WSL VHD full: heartbeat disk, refuse claim, alert (M13).
- Agent crash mid-job: supervisor restarts agent; new process reconciles; boot_session may or may not change. If supervisor restarts WSL, boot_session changes and the scheduler fences — correct. Prefer restarting only the agent process to keep the same session when the container is still running. Document: **agent process restart keeps session; WSL restart rotates session.**

## Implementation steps

1. Service account + NSSM/WinSW or native Windows Service wrapper.
2. Import a golden WSL `.wsl` distro.
3. Agent binary/script with register/heartbeat/claim loop (noop executor).
4. Reconcile + fencing self-terminate.
5. Install on one inventory PC.

## Acceptance criteria

- After Windows reboot with no user login, the node heartbeats within 2 minutes.
- `GET /nodes` shows inventory fields and eligibility.
- Leftover noop container from a killed agent is removed on restart.
- Node token is not visible in `docker inspect` of any job container.
- CUDA preflight failure prevents claiming.
