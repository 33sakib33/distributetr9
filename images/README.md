# Catalog images (M08)

v1 jobs run only these maintained images, pulled by digest. Do not use `:latest` on agents.

Until a digest is pinned in `control/app/seed.py`, the agent maps `sha256:dev-*` to `python:3.12.7-slim` (or `LAB_IMAGE`).

## Campus registry

`docker compose up -d registry` runs `registry:2` on host port 5001 (container still listens on 5000). Custom `requirements.txt` is rejected unless `REGISTRY_URL` is set. A **build** agent (`--role build`) pushes `REGISTRY_URL/lab-env/<hash>:ready`; **train** agents only `docker pull` the stored `name@sha256:…`.

```bash
scripts/mirror-catalog.sh 192.168.68.118:5001
```

On every lab Docker daemon (`/etc/docker/daemon.json`):

```json
{
  "insecure-registries": ["192.168.68.118:5001"]
}
```

Prove a second PC can pull the printed RepoDigest before turning custom environments on.

## CPU smoke image

```bash
docker build -t lab-cpu:local images/cpu
```

The agent already uses `python:3.12.7-slim` for placeholder catalog rows.

## PyTorch + CUDA 12.4

Build on a machine with an NVIDIA GPU (the Windows lab PC, inside WSL):

```bash
docker pull pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
docker inspect --format='{{index .RepoDigests 0}}' pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime
```

Put that `@sha256:…` digest into `environments.image_digest` (seed or admin update). Agents pull by digest and never retag.

TensorFlow follows the same pattern: pin a runtime image, store the digest, do not build student environments (that is M09).

## GPU flag

`--gpus device=0` is added only when `nvidia-smi` works **and** the job requested VRAM. Mac agents skip it.
