# Runbooks

Step-by-step procedures for running the lab platform. Each one names the
command to run and how to tell it worked. Commands assume the repo is checked
out on the lab server at `/srv/lab` and run from there.

```bash
P="docker compose -f deploy/docker-compose.prod.yml --env-file deploy/prod.env"
```

Rehearse anything risky on the simulated lab first (`make sim`, `make sim-exam`).

---

## 1. Stand up the lab server

Needs: an always-on Linux machine on the lab network, Docker Engine with the
compose plugin, three DNS names pointing at it (portal, API, registry), and a
campus OIDC client whose redirect URI is `https://<API_HOST>/auth/callback`.

1. `cp deploy/prod.env.example deploy/prod.env` and fill in every value.
   Generate secrets with `openssl rand -base64 36`. Hash the registry push
   password with `docker run --rm caddy:2.8.4 caddy hash-password --plaintext '<password>'`.
2. `$P up -d --build`
3. Check: `$P ps` shows every service running, and
   `curl -k https://<API_HOST>/health` returns `{"ok":true}`.
   If `control` keeps restarting, `$P logs control` lists any production
   setting still at a dev default ("refusing to start in production").
4. With `LAB_TLS=internal`, export Caddy's root CA for the lab PCs:
   `$P cp caddy:/data/caddy/pki/authorities/local/root.crt ./lab-ca.crt`
5. Sign in to the portal with an address in `ADMIN_EMAILS` and open the Nodes page.
6. Schedule backups (runbook 9) before anyone else uses it.

## 2. Add a lab PC

**One command** (Administrator PowerShell on the PC). It installs WSL Ubuntu,
creates the `lab` Linux user, installs Docker and the agent, installs the boot
task, and waits until the PC registers. NVIDIA GPUs are detected automatically.

Pilot server (plain HTTP); the server fills in its own API and registry address:

```powershell
powershell -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm http://<server-ip>:8080/install/bootstrap.ps1)))"
```

Production with `LAB_TLS=internal` (copy `lab-ca.crt` to `C:\lab\` first):

```powershell
curl.exe -fsSL --cacert C:\lab\lab-ca.crt -o C:\lab\bootstrap.ps1 https://<API_HOST>/install/bootstrap.ps1
powershell -ExecutionPolicy Bypass -File C:\lab\bootstrap.ps1 -CaCert C:\lab\lab-ca.crt
```

Options: `-Role build` for the builder PC, `-Gpu no` to ignore a GPU,
`-ResetIdentity` before capturing a disk image, `-NoBootTask` to start by hand.
If it says WSL was just enabled, reboot and run the same command again. It is
safe to re-run on a PC that is already set up.

Check: reboot without logging in; the PC appears on the Nodes page as
`eligible` with the expected GPU class within a minute or two.
Builder PC only: `wsl -d Ubuntu -u lab -- docker login <REGISTRY_HOST>` with the push user.

**Manual steps** (what the bootstrap does, for troubleshooting):

1. Install WSL Ubuntu (`wsl --install -d Ubuntu`) and create the lab user.
2. Copy the repo (or just `agent/`, `scripts/`, `supervisor/`) and `lab-ca.crt` onto the PC.
3. Inside WSL:
   ```bash
   sudo bash scripts/setup-lab-node.sh --api https://<API_HOST> \
     --registry <REGISTRY_HOST> --ca-cert lab-ca.crt --gpu
   ```
   Drop `--gpu` on machines without an NVIDIA card. Add `--role build` on the builder PC.
4. From Windows: `wsl --shutdown`, then install the boot task with
   `supervisor\install.cmd` (see `supervisor/README.md`).
5. Reboot without logging in. Check: the PC appears on the Nodes page as
   `eligible` with the expected GPU class within a minute or two.
6. Builder PC only: `docker login <REGISTRY_HOST>` with the push user.

**Disk imaging instead:** set up one PC, run the installer with
`--reset-identity` as the last step before capturing the image, and never
start the agent on the master. Each clone registers as a new node on first boot.

## 3. Update the agent (canary first)

Agents update themselves between jobs and roll back a release that cannot start.

1. Bump `AGENT_VERSION` in `agent/agent.py`, merge, and pull on the server.
2. Set `AGENT_ROLLOUT_RING=0` in `deploy/prod.env`, then `$P up -d --build control`.
3. Put one PC in ring 0: Nodes page, or
   `curl -X POST https://<API_HOST>/nodes/<id>/ring -d '{"ring":0}' ...` with an admin session.
4. Check: that PC shows the new version within a few minutes and keeps completing jobs.
   If it rolled back, its `/opt/lab-agent/rejected` lists the version and the
   launcher log says why.
5. Widen: `AGENT_ROLLOUT_RING=1` (early group), then `2` (everyone), restarting `control` each time.
6. Optional: once everyone is updated, set `AGENT_MIN_VERSION` so any PC that
   missed the update gets no work until it catches up.

## 4. Drain a PC for maintenance

1. Nodes page → Drain (or `POST /nodes/<id>/drain`). The running job finishes; no new work is given.
2. Check: the node's current job completes and no new attempt starts on it.
3. Do the maintenance (driver update, Windows update, disk cleanup). Restarts
   and reboots keep the node drained.
4. Bring it back: Nodes page → Release (or `POST /nodes/<id>/release`).

## 5. Quarantine a misbehaving PC

When alerts report repeated failures from one host, or a GPU is flaky:

1. Nodes page → Quarantine with a reason. Its active attempt is failed and
   retried elsewhere immediately.
2. Investigate on the PC: `nvidia-smi`, `docker info`, free disk, the launcher log.
3. Release it (Nodes page → Release) only after a successful manual CUDA check:
   `docker run --rm --gpus all pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime python -c "import torch; print(torch.cuda.is_available())"`

## 6. Replace a broken WSL install

1. Quarantine the node (runbook 5).
2. From Windows: `wsl --unregister Ubuntu`, then `wsl --install -d Ubuntu`.
3. Re-run runbook 2 from step 3, adding `--reset-identity`.
4. In the portal the old node entry stays offline; the PC registers as a new node.
   Revoke the old entry (runbook 8) so its token is dead.

## 7. Disk full on a PC

The agent stops claiming work when free space falls below `DISK_WATERMARK_BYTES`
and the alerts report it.

1. Inside WSL: `docker system df`, then `docker image prune -a --filter until=168h`.
2. Remove leftover attempt directories: `sudo find /var/lab/attempts -mindepth 1 -maxdepth 1 -mtime +2 -exec rm -rf {} +`
3. WSL disks grow but never shrink on their own. To return space to Windows:
   `wsl --shutdown`, then in an admin PowerShell
   `Optimize-VHD -Path <path-to-ext4.vhdx> -Mode Full` (Hyper-V module) or
   `diskpart` → `select vdisk file=<path>` → `compact vdisk`.

## 8. Rotate credentials

| What | How | Effect |
|---|---|---|
| One node's token (lost or stolen PC) | `POST /nodes/<id>/revoke` as admin | Node is quarantined and can never call back; reinstall with `--reset-identity` |
| `TOKEN_PEPPER` | Change in `prod.env`, `$P up -d control` | **Every** node token and session becomes invalid; all PCs must re-register (`--reset-identity`). Only after a server compromise |
| `OIDC_CLIENT_SECRET` | Rotate in the identity provider, update `prod.env`, restart `control` | Users sign in again |
| MinIO / Postgres passwords | Change inside the service first (`mc admin user` / `ALTER USER`), then `prod.env`, then `$P up -d` | Brief API restart |
| Registry push password | New hash in `prod.env`, `$P up -d caddy`, `docker login` again on the builder | Builds pause until re-login |
| Agent signing key | Avoid. A new key means every PC rejects updates until reinstalled. Keep the backup (runbook 9) | — |

## 9. Back up and restore

Backups cover Postgres, MinIO (inputs, logs, outputs) and the agent signing key.

```bash
deploy/backup.sh /srv/backups          # keeps 14 days by default (KEEP_DAYS)
# cron: 15 3 * * *  cd /srv/lab && deploy/backup.sh /srv/backups >> /var/log/lab-backup.log 2>&1
```

Copy `/srv/backups` off the machine (another server, a campus share).

Restore (stops the API while it runs):

```bash
deploy/restore.sh /srv/backups/20261101-031500
```

Check: job history and downloads in the portal match the backup time. **Test a
restore once a term** onto a spare machine; a backup never restored is a guess.

## 10. Clean up storage

```bash
$P exec control python -m app.ops gc --days 30 --dry-run    # see what would go
$P exec control python -m app.ops gc --days 30
$P exec registry registry garbage-collect --delete-untagged /etc/docker/registry/config.yml
```

Removes outputs of attempts that were never accepted, and custom environment
images no job used for 30 days (they rebuild on demand).

## 11. Daily health check

```bash
$P exec control python -m app.ops status
```

Prints node states, queue size and current alerts. The same alerts are posted
to `ALERT_WEBHOOK_URL` when set: offline PCs, low disk, lease losses, repeated
failures on one PC, and jobs queued too long.

## 12. The control API moved (new IP or hostname)

Agents keep working if they use a hostname. If the address itself changed:

1. Update the DNS names, or `api_base` in `/etc/lab-agent/config.json` on each PC.
2. A new registry address: update `REGISTRY_HOST`, restart `control`. Ready
   custom environments rebuild automatically on their next use.
