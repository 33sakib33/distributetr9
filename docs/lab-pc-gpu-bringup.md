---
title: Bringing a Lab GPU PC onto the Job Platform
subtitle: What happened, why it failed, and the theory behind each fix
author: distributetr9 lab notes
date: 19 August 2026
---

# What this system is

Two machines on the same Wi-Fi:

| Role | Machine | What runs there |
|------|---------|-----------------|
| Control plane | Mac (`192.168.68.118:8080`) | FastAPI, Postgres, MinIO. Students (later) submit jobs here. |
| Worker | Windows PC `DESKTOP-N7057V6` | NVIDIA GeForce RTX 3050 (~8 GB). WSL2 Ubuntu + Docker Engine. A pull-based Python agent claims jobs and runs one container per attempt. |

The portal is not involved yet. The gate for this stretch of work was: **a CUDA `train.py` runs on the 3050, exits 0, and uploads output to MinIO.**

Jobs never SSH into the PC. The agent polls the control API (`register`, `heartbeat`, `claim`, `renew`, `complete`). That is a **pull-based agent**.

------------------------------------------------------------------------

# Timeline: what you actually did

## 1. Classify the 3050 so it is allowed to claim work

**What you saw earlier.** The node registered as `unsupported` and `quarantined`. An 8 GB Ampere card sat below the old catalog floor (`ampere-12g` started at 10 240 MB). Unsupported class meant `cuda_preflight_ok` was false, so the scheduler would not give it GPU-class work.

**What you did.** Copied the updated `agent/` folder (`classes.json` included). Catalog now has `ampere-8g` (5 120–10 239 MB). After wiping old node rows and deleting `~/.distributetr9/agent-state.json`, the PC registered again.

**What the API showed.**

- Hostname `DESKTOP-N7057V6`
- GPU `NVIDIA GeForce RTX 3050`
- Class `ampere-8g`
- Eligibility `eligible`
- Agent `0.4.0`
- Allocatable VRAM 7936 MB (8192 − 256 MB driver reserve)

Docker without sudo also started working after `usermod -aG docker` and `wsl --shutdown` (or `sg docker`). That is a **Linux group / session** issue, not a Docker bug: `/var/run/docker.sock` is owned by `root:docker`; your user must be in `docker`, and the login session must be recreated so the kernel token includes that group.

## 2. First real claim: hello job, then permission denied

**What you did.** Submitted the CPU smoke job from the Mac (`python3 scripts/smoke.py`). The agent printed `claimed` plus a UUID. Then it printed a **network error** mentioning `/var/lab/attempts` and **permission denied (errno 13)**.

**What was really happening.** Staging writes the input archive under an attempt directory. The agent preferred `/var/lab/attempts` if that path existed. Install notes had created it as **root-owned mode 0700**. The agent runs as `sakib`, so `mkdir` failed.

`PermissionError` is a subclass of `OSError` in Python. The agent’s outer loop caught `OSError` and labelled it `network error`. The claim had already succeeded; only local disk setup failed. The attempt stayed leased until the lease expired, then retried.

**What you did to fix it.**

```bash
sudo chown $USER:$USER /var/lab/attempts
sudo chmod 700 /var/lab/attempts
```

After that, attempt 2 of the same job ran. Docker pulled `python:3.12.7-slim` (catalog placeholders `sha256:dev-*` map to that image). Job **completed, exit 0**. That proved: LAN API, claim, fencing token, container start, logs, output upload.

## 3. CUDA path: Docker could not see a GPU vendor

**What you did.** Pulled `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`, set `LAB_IMAGE` to that tag, started the agent, submitted `LAB_SMOKE=cuda python3 scripts/smoke.py`.

**What failed.** `docker run --gpus …` said **no known GPU vendor found** (CDI: Container Device Interface). `nvidia-smi` in WSL can still work.

**Theory.** WSL2 GPU is a **split stack**:

1. **Windows NVIDIA driver** exposes the GPU into WSL (`nvidia-smi` in Ubuntu). You must **not** install a Linux `nvidia-driver-*` package inside WSL.
2. **Docker Engine** does not talk to that driver by itself. `--gpus` needs the **NVIDIA Container Toolkit** (`nvidia-container-toolkit`) inside the distro.
3. Modern Docker discovers GPUs via **CDI** YAML (typically `/etc/cdi/nvidia.yaml`). Empty or missing spec means Docker reports “no known GPU vendor”.

**What you installed (each step’s job).**

| Command | Role |
|---------|------|
| `apt-get update` / install `ca-certificates curl gnupg2` | HTTPS and apt tooling |
| NVIDIA GPG key into `/usr/share/keyrings/...gpg` | Trust NVIDIA’s package repo |
| Rewrite and write `nvidia-container-toolkit.list` | apt source, signed with that key |
| `apt-get install nvidia-container-toolkit` | Runtime + `nvidia-ctk` |
| `nvidia-ctk runtime configure --runtime=docker` | Patch `/etc/docker/daemon.json` so Docker knows the NVIDIA runtime |
| `nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` | Write the vendor spec Docker was missing |
| `nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place` | WSL cgroups are incomplete; device cgroup enforcement often breaks GPU containers |
| `service docker restart` | Load the new daemon config |

After that, this succeeded:

```bash
docker run --rm --gpus all pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime \
  python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

Output: `True` and `RTX 3050`. That is **GPU-in-container as root**, not yet “the agent’s job container”.

## 4. Smoke CUDA: torch is CUDA-built, but `cuda_available False`

**What the job logs showed.**

```
torch 2.5.1+cu124
cuda_available False
```

Exit code 1. So:

- `LAB_IMAGE` was in use (the slim CPU image does not ship that torch wheel).
- The script imported PyTorch **compiled with CUDA 12.4**.
- The process still could not see a device.

**Theory.** `torch.cuda.is_available()` is not “is this a CUDA wheel?”. It is “can this process open NVIDIA devices **right now**?” A CUDA wheel on a CPU-only container (no `--gpus`) prints exactly this: version `+cu124`, availability `False`.

Your working command was:

- `--gpus all`
- default user **root**
- no `--read-only` / `--user`

The agent was launching:

- `--gpus device=0` — on WSL + CDI this often does not map the same way as `all`
- `--user 10000:10000` — isolation: jobs must not run as root. Device nodes under `/dev/nvidia*` are typically not usable by uid 10000, so CUDA init fails quietly and PyTorch reports no GPU.

Optional reproduction:

```bash
docker run --rm --gpus all --user 10000:10000 pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime \
  python -c "import torch; print(torch.cuda.is_available())"
```

That often prints `False` even when the same image as root prints `True`.

**What we changed in `executor.py`.** For GPU jobs (`min_vram_mb > 0` and `nvidia-smi` works):

- `--gpus all`
- `NVIDIA_VISIBLE_DEVICES=all` and `NVIDIA_DRIVER_CAPABILITIES=compute,utility`
- **do not** pass `--user 10000:10000`

CPU jobs still use uid 10000, `--read-only`, `--network none`, no docker.sock. After recopying `executor.py` and rerunning CUDA smoke, the job completed.

------------------------------------------------------------------------

# Theories you need in order to reason about this

## Pull-based execution and leases

The control plane never pushes a process onto the PC. The agent **claims** an attempt and receives a **fencing token**. Only that token can complete or upload. If the agent dies after claim, the **lease** expires, the attempt is marked lost, and the job may retry (hello job: attempt 1 failed on disk, attempt 2 completed).

That is why “claimed then crashed” is not “job gone forever”. It is “this try is fenced; another try can start”.

## Compatibility class vs “it has a GPU”

Scheduling matches `gpu_class` (e.g. `ampere-8g`) to `nodes.compatibility_class`. A 3050 that reports `unsupported` is treated like a quarantined host. Inventory bands exist so a 24 GB card is not interchangeable with an 8 GB card, and so old GTX cards stay out of the pool.

VRAM in the catalog is **total** MB from `nvidia-smi`. Allocatable is total minus 256 MB. The 3050 8 GB band is `ampere-8g`.

## Three layers of “Docker can use the GPU”

Think of three independent yes/no questions:

1. **Host / WSL:** `nvidia-smi` — Windows driver + WSL GPU paravirt.
2. **Daemon:** NVIDIA Container Toolkit + CDI spec + docker restart — `--gpus` is meaningful.
3. **This container process:** flags and uid — devices actually appear and are openable.

You failed (2), then (3), while (1) was already true. Mixing those three is the usual source of “but nvidia-smi works” confusion.

## CDI vs the old NVIDIA runtime hook

Older setups injected GPUs with `nvidia-container-runtime` hooks. Newer Docker often asks CDI: “which GPU vendors are on this machine?” If `/etc/cdi/nvidia.yaml` (or the runtime equivalent) is missing, you get **no known GPU vendor**, even with the toolkit half-installed. `nvidia-ctk cdi generate` writes that file.

## Linux credentials: groups, uids, errno 13

- **docker group:** access to the daemon socket. New group membership needs a new session (`wsl --shutdown` or `sg docker`).
- **uid 10000 in the job:** the process inside the container is not your WSL user and not root. GPU device nodes often require root or the `video` group.
- **errno 13 / EACCES:** the kernel refused a filesystem operation. In Python, `PermissionError` is a subclass of `OSError`. Catching “all OSError as network” hid a local chmod problem.

## Images and `LAB_IMAGE`

Catalog rows still use placeholder digests `sha256:dev-pytorch-local`. The agent maps those to `python:3.12.7-slim` unless `LAB_IMAGE` overrides. CUDA smoke must run in a CUDA image; the hello job correctly used slim. Pinning a real digest in Postgres is later work; the env override was the lab shortcut.

## Isolation flags that stayed in place

Even after the GPU exception, jobs still get:

- `--network none` — no outbound network from student code
- `--read-only` — container rootfs not writable (output goes to a mounted `/output`)
- no docker.sock mount — the job cannot start sibling containers
- cgroup CPU/RAM/PID limits

The GPU exception is only: attach all GPUs, and do not drop to uid 10000 for that class of job.

------------------------------------------------------------------------

# Technical map of the stack (WSL)

```
Windows NVIDIA driver
        |
        v
WSL2 (Ubuntu)  -- nvidia-smi
        |
        +-- Docker Engine  -- /var/run/docker.sock (group docker)
        |         |
        |         v
        |   NVIDIA Container Toolkit + CDI (nvidia.yaml)
        |         |
        |         v
        |   docker run --gpus all  -->  /dev/nvidia* in the container
        |         |
        |         v
        |   PyTorch cu124  -->  torch.cuda.is_available()
        |
        +-- lab agent (Python)  -- claims jobs, docker run ...
```

Control plane stays on the Mac. The PC only needs TCP 8080 to `192.168.68.118`.

------------------------------------------------------------------------

# What is proven vs what is next

**Proven on this PC**

- Node registers as `ampere-8g`, eligible, heartbeats.
- Docker Engine usable without sudo.
- CPU job in slim: claim, run, complete, MinIO.
- Toolkit + CDI: `docker run --gpus all` sees the 3050.
- CUDA smoke in the PyTorch image: `cuda_available True`, exit 0 (after executor change).

**Not done**

- Pin the image **digest** in `environments.image_digest` (tags can move).
- Windows scheduled task so the agent starts at boot with nobody logged in.
- Portal (Next.js). Spec still says: GPU job over the API first — that gate is now met.
- University OIDC, Alembic, ops runbooks, fleet rollout.

------------------------------------------------------------------------

# Copy of the working CUDA agent start

On the lab PC, after toolkit install and a writable `/var/lab/attempts`:

```bash
export LAB_IMAGE=pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
LAB_API=http://192.168.68.118:8080 bash ~/distributetr9_node/run.sh
```

On the Mac:

```bash
LAB_SMOKE=cuda python3 scripts/smoke.py
```

Expect logs containing `executor docker image=pytorch/pytorch:… gpus=yes` and `cuda_available True`.
