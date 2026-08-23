#!/usr/bin/env bash
# Run inside the lab WSL distro after copying agent/ to /opt/lab-agent
# (or ~/distributetr9_node).
set -euo pipefail
API="${1:-http://127.0.0.1:8080}"
AGENT_SRC="${2:-}"

if [ -z "$AGENT_SRC" ]; then
  if [ -f /opt/lab-agent/agent.py ]; then
    AGENT_SRC=/opt/lab-agent
  elif [ -f "$HOME/distributetr9_node/agent.py" ]; then
    AGENT_SRC="$HOME/distributetr9_node"
  else
    echo "copy agent/ first: /opt/lab-agent or ~/distributetr9_node" >&2
    exit 1
  fi
fi

    if [ "$(id -u)" -eq 0 ]; then
  install -d -m 700 /etc/lab-agent /var/lab/attempts /opt/lab-agent
  chown "$SUDO_USER:$SUDO_USER" /var/lab/attempts 2>/dev/null || true
  DEST=/opt/lab-agent
  if [ "$AGENT_SRC" != "$DEST" ]; then
    cp -R "$AGENT_SRC"/. "$DEST"/
  fi
  chmod +x "$DEST/run.sh" 2>/dev/null || true
  cat > /etc/lab-agent/config.json <<EOF
{
  "api_base": "${API}",
  "executor": "docker"
}
EOF
  chmod 400 /etc/lab-agent/config.json
else
  mkdir -p "$HOME/.distributetr9"
  cat > "$HOME/.distributetr9/config.json" <<EOF
{
  "api_base": "${API}",
  "executor": "auto"
}
EOF
  chmod 600 "$HOME/.distributetr9/config.json"
  echo "wrote $HOME/.distributetr9/config.json (run as root to use /etc/lab-agent)"
fi

if command -v docker >/dev/null 2>&1; then
  sudo groupadd -f docker
  sudo usermod -aG docker "$USER" || true
  echo "docker group updated; from Windows run: wsl --shutdown"
fi

echo "start: bash ${AGENT_SRC}/run.sh"
echo "or:    python3 ${AGENT_SRC}/agent.py --api ${API} --executor python"
