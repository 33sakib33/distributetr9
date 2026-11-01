#!/bin/bash
# Boot sequence of a simulated lab PC. `docker restart` replays it like a
# reboot: the install and node identity survive, running jobs do not.
set -u

start_dockerd() {
  # `docker restart` keeps /var/run, so the old daemon's pid files are still
  # there and the new dockerd refuses to start next to a "running" one.
  rm -f /var/run/docker.pid /var/run/docker/containerd/containerd.pid
  dockerd-entrypoint.sh dockerd --insecure-registry "${SIM_REGISTRY:-registry:5000}" >>/var/log/dockerd.log 2>&1 &
}

docker_ready() {
  for _ in $(seq 1 60); do
    timeout 5 docker info >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

start_dockerd
if ! docker_ready; then
  echo "entry: dockerd did not come up within 60 s; restarting it once" >&2
  pkill -x dockerd
  pkill -x containerd
  sleep 3
  start_dockerd
  docker_ready || echo "entry: dockerd is still not up; continuing without it" >&2
fi

ROOT=/opt/lab-agent
if [ ! -e "$ROOT/current" ]; then
  VERSION="$(sed -n 's/^AGENT_VERSION = "\(.*\)"/\1/p' /src/agent/agent.py)"
  # SIM_START_VERSION installs an older-labelled copy so the node self-updates.
  VERSION="${SIM_START_VERSION:-$VERSION}"
  mkdir -p "$ROOT/releases/$VERSION"
  cp /src/agent/*.py /src/agent/*.json /src/agent/*.sh "$ROOT/releases/$VERSION/"
  sed -i "s/^AGENT_VERSION = .*/AGENT_VERSION = \"$VERSION\"/" "$ROOT/releases/$VERSION/agent.py"
  ln -sfn "releases/$VERSION" "$ROOT/current"
  cp /src/agent/launcher.sh "$ROOT/launcher.sh"
fi

mkdir -p /etc/lab-agent /var/lab/attempts /sim
cat > /etc/lab-agent/config.json <<JSON
{"api_base": "${LAB_API}", "executor": "docker", "role": "${LAB_NODE_ROLE:-train}"}
JSON

# Chaos knobs the exam toggles with `docker exec`.
export LAB_FAKE_BUSY_FILE=/sim/busy
export LAB_FAKE_DISK_FREE_FILE=/sim/disk_free
export LAB_ATTEMPT_ROOT=/var/lab/attempts
export LAB_AGENT_RESTART_DELAY="${LAB_AGENT_RESTART_DELAY:-2}"
exec bash "$ROOT/launcher.sh"
