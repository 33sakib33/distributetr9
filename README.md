# Lab ML job platform

Turns a lab of Windows PCs with consumer GPUs into a shared GPU cluster.
Students submit a Python training script in the portal; an idle lab PC runs it
in a Docker container under WSL, streams the logs, and returns the outputs.

- **control/**: FastAPI control plane (Postgres, MinIO, scheduler, agent API)
- **agent/**: stdlib-only agent for each lab PC, self-updating
- **portal/**: Next.js web UI
- **sim/**: a simulated five-PC lab in Docker, plus the scripted failure exam
- **deploy/**: production stack (HTTPS via Caddy), backups, restore
- **scripts/**: dev launcher, lab PC installer, smoke test

Status: [`PROGRESS.md`](PROGRESS.md). Design: [`spec/README.md`](spec/README.md).
Operations: [`docs/runbooks.md`](docs/runbooks.md). Registry background:
[`docs/image-registry.md`](docs/image-registry.md).

## Develop on one machine

Needs Docker, Python 3.12 and Node 22.

```bash
python3 -m venv control/.venv && control/.venv/bin/pip install -r control/requirements.txt
make dev        # Postgres, MinIO, registry, API :8080, portal :3000, a train + build agent
```

Open http://127.0.0.1:3000 and sign in with any `@uni.edu` address (a mock
campus login; `admin@uni.edu` sees the Nodes page). `python3 scripts/smoke.py`
submits a test job from the command line. Logs are in `.dev/logs/`.

## Tests

```bash
make test       # starts Postgres + MinIO, runs pytest against its own lab_test database
make lint
```

Schema changes go through Alembic:

```bash
cd control && ../control/.venv/bin/alembic revision --autogenerate -m "what changed"
```

The API applies migrations on startup.

## Simulated lab

Five lab PCs as containers, each with its own Docker daemon and fake GPU
(2 × `ampere-8g`, `ada-12g`, `ada-24g`, plus a builder), against a
containerized control plane on port 8090. One node starts on an old agent
version and updates itself.

```bash
make sim        # build and start
make sim-exam   # kill agents, reboot PCs, cut the network, fill disks, cancel, time out...
make sim-down
```

`control/.venv/bin/python -m sim.exam reboot network_drop` runs single
scenarios. The exam also runs nightly in CI.

## Production

On an always-on Linux server in the lab:

```bash
cp deploy/prod.env.example deploy/prod.env      # fill in every value
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/prod.env up -d --build
```

The API refuses to start while any dev default (dev login, default passwords,
mock identity provider) is still set. Full procedure, including backups and
updates: [`docs/runbooks.md`](docs/runbooks.md).

## Lab PCs

One command in an Administrator PowerShell on each PC sets up WSL, Docker, the
agent and the boot task, and waits until the PC registers:

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm http://<server>:8080/install/bootstrap.ps1)))"
```

For HTTPS with the lab's own CA, see runbook 2 in
[`docs/runbooks.md`](docs/runbooks.md). From a copy of the repo instead:
`supervisor\bootstrap.cmd -Api http://<server>:8080 -Registry <server>:5001`.
After that, agents update themselves from the control plane (signed bundles,
canary rings, automatic rollback).

Manual install inside WSL Ubuntu, if you prefer:

```bash
sudo bash scripts/setup-lab-node.sh --api https://<api-host> --registry <registry-host> --ca-cert lab-ca.crt --gpu
```

For a quick test against a laptop instead of a server:

```bash
sudo bash scripts/setup-lab-node.sh --api http://<laptop-ip>:8080 --registry <laptop-ip>:5001
lab-agent
```

The laptop's firewall must allow TCP 8080 and 5001, and both machines must be
on the same network (not a guest Wi-Fi with client isolation).

## Useful endpoints

| Method | Path | Who |
|--------|------|-----|
| GET | `/health` | anyone |
| POST | `/jobs` | user (multipart; optional `Idempotency-Key`) |
| GET | `/jobs/{id}`, `/jobs/{id}/logs/stream` | user (SSE logs) |
| GET | `/nodes`, `/admin/alerts` | admin |
| POST | `/nodes/{id}/drain`, `quarantine`, `release`, `ring`, `revoke` | admin |
| GET | `/agent/bundle-key`, `/agent/bundle` | agents |
| GET | `/install/bootstrap.ps1`, `/install/lab-node.tar.gz` | new lab PCs |
