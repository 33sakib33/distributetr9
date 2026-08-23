#!/usr/bin/env bash
# Start the lab agent from WSL. Prefer Docker via the docker group without a new login.
set -u

API="${LAB_API:-http://127.0.0.1:8080}"
CONFIG="${LAB_CONFIG:-/etc/lab-agent/config.json}"
EXECUTOR="${LAB_EXECUTOR:-auto}"

resolve_dir() {
  if [ -n "${LAB_AGENT_DIR:-}" ] && [ -f "${LAB_AGENT_DIR}/agent.py" ]; then
    echo "${LAB_AGENT_DIR}"
    return
  fi
  local here
  here="$(cd "$(dirname "$0")" && pwd)"
  if [ -f "$here/agent.py" ]; then
    echo "$here"
    return
  fi
  if [ -f "$here/agent/agent.py" ]; then
    echo "$here/agent"
    return
  fi
  for cand in /opt/lab-agent "$HOME/distributetr9_node" "$HOME/distributetr9_node/agent" "$HOME/distributetr9/agent"; do
    if [ -f "$cand/agent.py" ]; then
      echo "$cand"
      return
    fi
  done
  echo "agent.py not found (set LAB_AGENT_DIR)" >&2
  exit 1
}

in_docker_group() {
  local user
  user="$(id -un)"
  id -nG 2>/dev/null | grep -qw docker && return 0
  getent group docker 2>/dev/null | grep -qw "$user"
}

start_docker() {
  command -v docker >/dev/null 2>&1 || return 0
  if command -v service >/dev/null 2>&1; then
    sudo -n service docker start >/dev/null 2>&1 || true
  elif command -v systemctl >/dev/null 2>&1; then
    sudo -n systemctl start docker >/dev/null 2>&1 || true
  fi
}

DIR="$(resolve_dir)"
cd "$DIR" || exit 1
start_docker

run_agent() {
  local mode="$1"
  exec python3 "$DIR/agent.py" --config "$CONFIG" --api "$API" --executor "$mode"
}

if command -v sg >/dev/null 2>&1 && in_docker_group; then
  if sg docker -c "docker info" >/dev/null 2>&1; then
    exec sg docker -c "python3 '$DIR/agent.py' --config '$CONFIG' --api '$API' --executor docker"
  fi
fi

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  run_agent docker
fi

if [ "$EXECUTOR" = "docker" ]; then
  echo "docker executor requested but docker info failed" >&2
  docker info >&2 || true
  exit 1
fi

run_agent python
