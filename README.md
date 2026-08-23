# Lab ML job platform

Control API + Postgres + MinIO on this machine. Agents on any desktop on the same Wi-Fi.

Build status: [`PROGRESS.md`](PROGRESS.md). Spec: [`spec/README.md`](spec/README.md). Image registry (why, theory, how): [`docs/image-registry.md`](docs/image-registry.md).

## Start the control plane

Keep Postgres and MinIO in Docker, and run the API on the Mac host. Docker-published ports often fail from other machines with `No route to host` because of the macOS firewall.

```bash
docker compose up -d postgres minio registry
./scripts/run-control-lan.sh
```

API: `http://<this-Mac-Wi-Fi-IP>:8080` (currently `192.168.68.118`)
Health: `curl http://127.0.0.1:8080/health`

Registry (image distribution): `registry:2` on host port **5001** (5000 is AirPlay / Control Center on macOS). Set `REGISTRY_URL=<reachable-host>:5001` before custom `requirements.txt` will be accepted. Mirror the catalog image with `scripts/mirror-catalog.sh`. Lab Docker daemons need `"insecure-registries": ["<host>:5001"]` in `/etc/docker/daemon.json`. If Docker-published 5001 is unreachable from other PCs (same macOS firewall issue as 8080), run the registry on a Linux host agents can actually hit.

After switching Wi-Fi, get the new IP with `ipconfig getifaddr en0` and point the desktop agent at that.

If macOS asks to allow incoming connections for Python, click **Allow**.

If the other desktop still gets `No route to host`, both machines are probably isolated on Wi-Fi (guest network / AP isolation). Put them on the same main SSID, not guest.

## Submit a job (this Mac)

```bash
python3 scripts/smoke.py
# GPU job for the 3050 node (after it is registered as ampere-8g):
# LAB_SMOKE=cuda python3 scripts/smoke.py
```

Dev auth header is `X-Dev-User: alice@uni.edu` when `DEV_AUTH=1` (tests, smoke, harness). The portal uses university OIDC.

## Portal

```bash
./scripts/run-portal.sh
```

Open `http://127.0.0.1:3000`, click Sign in, enter your university email. Use `admin@uni.edu` if you need the Nodes page.

## Run an agent (this Mac or the other desktop)

Copy the whole `agent/` folder (not only `agent.py`) to the other machine, then:

```bash
python3 agent/agent.py --api http://192.168.68.118:8080
# Environment builder (does not take training jobs):
# python3 agent/agent.py --api http://192.168.68.118:8080 --role build --executor docker
```

`--executor auto` (default) uses Docker when the daemon is reachable, otherwise local Python. Force one or the other with `--executor docker` or `--executor python`. On WSL, prefer `bash agent/run.sh` so the process runs in the `docker` group even before a new login.

The agent classifies the GPU (`ampere-8g`, `ada-12g`, `cpu`, …) instead of a generic `gpu` label. It skips claims when CUDA preflight fails, the node is draining/quarantined, or the desktop GPU looks busy.

The PyTorch catalog environment is `pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime`. `LAB_IMAGE` still overrides. On start, a Docker agent removes leftover `lab-*` containers.

Windows lab PC: copy `supervisor/` as well and follow [`supervisor/README.md`](supervisor/README.md). Probe the machine first:

```bash
python3 inventory/probe.py --append inventory/lab.jsonl
```

If macOS or Windows firewall blocks port 8080, allow incoming TCP 8080 on this Mac. The registry needs TCP 5001 as well.

Live logs: `GET /jobs/{id}/logs/stream` (SSE). Reconnect with `Last-Event-ID` set to the last seq.

## Tests

Postgres and MinIO must be up. Then:

```bash
docker compose up -d postgres minio
control/.venv/bin/pytest tests -v
```

Optional stress (needs the control API running):

```bash
python -m harness --api http://127.0.0.1:8080 --agents 100 --jobs 500 --seconds 60
```

## Useful endpoints

| Method | Path | Auth |
|--------|------|------|
| GET | `/health` | none |
| GET | `/me` | session cookie or `X-Dev-User` |
| GET | `/auth/login` | OIDC redirect |
| GET | `/environments` | none |
| POST | `/projects` | `X-Dev-User` |
| POST | `/jobs` | multipart + `X-Dev-User` |
| GET | `/jobs/{id}` | `X-Dev-User` |
| GET | `/jobs/{id}/logs/stream` | `X-Dev-User` (SSE) |
| GET | `/nodes` | admin `X-Dev-User` |
| POST | `/agent/register` | none first time |
