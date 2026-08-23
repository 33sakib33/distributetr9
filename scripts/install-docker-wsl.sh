#!/usr/bin/env bash
# One-time Docker Engine group membership for the lab WSL agent.
# Run inside WSL. Does not install Docker Desktop.
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "install Docker Engine in this WSL distro first (not Docker Desktop)" >&2
  exit 1
fi

sudo groupadd -f docker
sudo usermod -aG docker "$USER"

if command -v service >/dev/null 2>&1; then
  sudo service docker start >/dev/null 2>&1 || true
elif command -v systemctl >/dev/null 2>&1; then
  sudo systemctl start docker >/dev/null 2>&1 || true
fi

echo "added $USER to group docker"
echo "from Windows PowerShell run:  wsl --shutdown"
echo "then open a new WSL terminal so 'groups' includes docker"
echo "until then this works:  sg docker -c 'docker info'"

if sg docker -c "docker info" >/dev/null 2>&1; then
  echo "docker info: ok (via sg docker)"
else
  echo "docker info still failing; paste the next command's output:" >&2
  sg docker -c "docker info" || docker info || true
  exit 1
fi
