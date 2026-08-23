# 00 — Product

A campus lab of Windows PCs with consumer GPUs runs student and researcher ML jobs. Users submit Python work through a portal. A control plane queues that work. A Windows supervisor plus a WSL agent on each PC claims a job, runs it in one Docker container, streams logs, and uploads outputs.

The hard problem is not the portal. It is keeping exactly one accepted result when nodes disconnect, leases expire, completions are retried, and cancellation races completion.

## Users

| Actor | What they do |
|-------|----------------|
| Student / researcher | Log in with university identity, belong to projects, submit jobs, watch logs, download outputs, cancel, retry |
| Project owner | Same as a member, plus project membership and concurrency visibility |
| Lab administrator | Inventory PCs, drain and quarantine nodes, maintain base images, roll updates, restore storage |
| Platform operator | Same as admin during v1; later split if needed |

## First-release freeze (v1)

Ship only this:

- University login and user/project accounts
- Python scripts only (no notebooks)
- One input archive and one output directory
- Maintained PyTorch and TensorFlow images (no arbitrary `requirements.txt` builds)
- One GPU per job, one job per PC, single-node training
- Resource request: GPU class / min VRAM, CPU, RAM, disk, max runtime
- States: submitted, validating, queued, leased, starting, running, uploading, completed, failed, cancelling, cancelled, unknown
- Stream stdout/stderr
- Cancel queued or running jobs
- Download logs and output files
- Per-user and per-project concurrency limits
- Understandable queue reasons, e.g. `waiting for a 12 GB GPU`
- Job history

Explicitly later (not v1):

- Notebook execution and executed-notebook download
- User-submitted `requirements.txt` / `environment.yml` builds
- Checkpoint upload during long jobs and retry-from-checkpoint
- Distributed training
- Metrics UI beyond what logs already contain
- Non-Docker (systemd/conda) executor
- Resource-usage reports beyond attempt-level counters

## Inventory gate

Before scheduler work on real hardware, every lab PC is inventoried (M03). Unsupported or unreliable GPU classes are quarantined. Compatibility classes (CUDA / GPU arch / driver) are named and frozen for v1 image catalogs.

## Success

v1 is successful when:

- A student can submit a PyTorch script, see it run on a lab PC, stream logs, and download outputs.
- Killing the agent, WSL, or the network never produces two accepted results or a silently lost job.
- Cancellation is eventually enforced.
- Administrators can drain, quarantine, and see why a node is ineligible.
