# Windows supervisor + WSL agent

The control plane stays on the Mac. This folder is what you copy onto a **Windows lab PC** on the same Wi-Fi.

**Preferred first install (no boot start):** from this repo in WSL, `sudo bash scripts/setup-lab-node.sh --api http://<mac-ip>:8080`. Or from Windows: `scripts\setup-lab-node.cmd --api http://<mac-ip>:8080`. Then start with `lab-agent`. Use the Scheduled Task below only when you want the agent after reboot.

Agent process restart keeps `boot_session_id`. Restarting the WSL distro rotates the session and fences any in-flight attempt — that is correct.

**Self-updating installs:** when `setup-lab-node.sh` has installed `/usr/local/bin/lab-agent` and `/opt/lab-agent/launcher.sh`, the scheduled task runs that launcher. It starts `/opt/lab-agent/current`, applies signed updates from the control plane, and rolls back a release that will not start. The manual copy steps below are only for machines without the installer.

Canonical Windows path for these scripts: **`C:\lab\supervisor\`** (SYSTEM must still be able to read `run-wsl-agent.ps1` after reboot). The Python agent lives in WSL, typically `/opt/lab-agent` or `/home/<user>/distributetr9_node`.

## One-time install (admin PowerShell)

1. Install WSL2, NVIDIA driver, and Docker Engine **inside WSL** (not Docker Desktop).
2. Copy the whole `agent/` folder into the distro (all of `agent.py`, `executor.py`, `classify.py`, `classes.json`, `run.sh`):

```powershell
wsl -d Ubuntu -- mkdir -p /opt/lab-agent /etc/lab-agent /var/lab/attempts
wsl -d Ubuntu -- bash -lc "chmod 700 /etc/lab-agent; chown \$USER:\$USER /var/lab/attempts; chmod 700 /var/lab/attempts"
```

From this repo (adjust the UNC/path):

```powershell
wsl -d Ubuntu -- bash -lc "cp -R /mnt/c/lab/distributetr9/agent/. /opt/lab-agent/"
```

If you already keep the agent at `~/distributetr9_node`, copy the same files there. `run-wsl-agent.ps1` looks in `/opt/lab-agent`, then `$HOME/distributetr9_node`.

3. Add the WSL user to the `docker` group and prove `docker info` works without sudo:

```powershell
wsl -d Ubuntu -- bash -lc 'sudo groupadd -f docker && sudo usermod -aG docker $USER'
```

Inside WSL (after copying `scripts/` or from the repo):

```bash
bash scripts/install-docker-wsl.sh
```

Then from Windows: `wsl --shutdown`. Open a new WSL shell and check `groups` includes `docker`, or run `sg docker -c "docker info"`.

Until `docker info` exits 0, `run.sh` starts the agent with the Python executor.

4. Write `/etc/lab-agent/config.json` (mode 0400, root-owned). Use the Mac LAN IP:

```json
{
  "api_base": "http://192.168.68.118:8080",
  "executor": "docker"
}
```

```powershell
wsl -d Ubuntu -- bash -lc "chmod 400 /etc/lab-agent/config.json"
```

5. Register a boot-start task that does not need an interactive login.

Windows often blocks `.ps1` files (`running scripts is disabled`). Do **not** run `.\install.ps1` by itself. Use Bypass, or the `.cmd` wrapper, in an **Administrator** prompt.

Do **not** pass a Linux path starting with `/`. PowerShell treats `/home` as extra switches, including inside the scheduled task. Install writes `C:\lab\supervisor\settings.json` instead:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\lab\supervisor\install.ps1 -Distro Ubuntu -Api http://192.168.68.118:8080 -AgentDir home/sakib/distributetr9_node
```

Or:

```text
C:\lab\supervisor\install.cmd Ubuntu http://192.168.68.118:8080 home/sakib/distributetr9_node
```

Optional, so `.ps1` works in your own sessions later:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

The Linux path is `/home/sakib/distributetr9_node` if `agent.py` / `run.sh` are there, or that folder plus `/agent` if you copied the repo’s `agent/` directory inside it. The supervisor probes both.

`install.ps1` creates a Scheduled Task `LabMlAgent` as the **installing Windows user** (S4U, Highest). SYSTEM cannot start WSL2 on most PCs. After boot it waits 45s, then loops `start-agent.sh` inside WSL. Log: `C:\lab\supervisor\lab-agent.log`.

## Check

On the Mac:

```bash
curl -H "X-Dev-User: admin@uni.edu" http://127.0.0.1:8080/nodes
```

The Windows hostname should appear with a real compatibility class (`ampere-8g` for an RTX 3050 8 GB, `ada-12g`, …), not `gpu` and not `unsupported`. After a reboot with nobody logged in, a heartbeat should arrive within two minutes.

## Optional: WinSW service

If you prefer a Windows service over a scheduled task, download [WinSW](https://github.com/winsw/winsw/releases) as `supervisor/WinSW.exe`, copy `LabAgent.xml` beside it, edit the distro/API, then:

```powershell
.\WinSW.exe install LabAgent.xml
.\WinSW.exe start LabAgent.xml
```
