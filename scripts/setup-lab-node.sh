#!/usr/bin/env bash
# One-shot lab node install inside WSL Ubuntu (or Debian).
# Installs Docker Engine, writes /etc and /var with readable perms, copies the
# agent. Does NOT install a boot service — start the agent yourself.
#
# Usage (from the repo, inside WSL):
#   sudo bash scripts/setup-lab-node.sh --api http://10.10.126.226:8080
#   sudo bash scripts/setup-lab-node.sh --api http://10.10.126.226:8080 --registry 10.10.126.226:5001
#   sudo bash scripts/setup-lab-node.sh --api http://HOST:8080 --role build --gpu
set -euo pipefail

API=""
REGISTRY=""
ROLE="train"
EXECUTOR="docker"
WANT_GPU=0
AGENT_SRC=""

usage() {
  sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
  echo "Flags: --api URL  --registry HOST:PORT  --role train|build  --executor docker|auto"
  echo "       --src DIR  --gpu  -h"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --api) API="${2:-}"; shift 2 ;;
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --executor) EXECUTOR="${2:-}"; shift 2 ;;
    --src) AGENT_SRC="${2:-}"; shift 2 ;;
    --gpu) WANT_GPU=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$API" ]; then
  echo "missing --api http://<control-host>:8080" >&2
  exit 2
fi
case "$ROLE" in train|build) ;; *) echo "role must be train or build" >&2; exit 2 ;; esac
case "$EXECUTOR" in docker|auto|python) ;; *) echo "executor must be docker, auto, or python" >&2; exit 2 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  exec sudo env LAB_USER="$(id -un)" "$0" "$@"
fi

LAB_USER="${LAB_USER:-${SUDO_USER:-}}"
if [ -z "$LAB_USER" ] || [ "$LAB_USER" = "root" ]; then
  echo "run via sudo from a normal user so files can be owned by that user" >&2
  exit 1
fi
LAB_HOME="$(getent passwd "$LAB_USER" | cut -d: -f6)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HERE="$(cd "$SCRIPT_DIR/.." && pwd)"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing command: $1" >&2
    exit 1
  }
}

is_wsl() {
  grep -qiE 'microsoft|wsl' /proc/version 2>/dev/null
}

apt_install() {
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends "$@"
}

start_docker() {
  if command -v service >/dev/null 2>&1; then
    service docker start >/dev/null 2>&1 || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    systemctl start docker >/dev/null 2>&1 || true
    systemctl enable docker >/dev/null 2>&1 || true
  fi
}

install_docker_engine() {
  if command -v docker >/dev/null 2>&1; then
    echo "docker already installed: $(docker --version 2>/dev/null || true)"
    start_docker
    return 0
  fi
  echo "installing Docker Engine (not Docker Desktop)"
  apt_install ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  . /etc/os-release
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}"
  if [ -z "$codename" ]; then
    echo "could not detect Ubuntu codename; install Docker Engine by hand" >&2
    exit 1
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${codename} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  start_docker
}

merge_insecure_registry() {
  local host="$1"
  python3 - "$host" <<'PY'
import json, pathlib, sys
host = sys.argv[1]
path = pathlib.Path("/etc/docker/daemon.json")
data = {}
if path.exists() and path.stat().st_size:
    data = json.loads(path.read_text() or "{}")
regs = list(data.get("insecure-registries") or [])
if host not in regs:
    regs.append(host)
data["insecure-registries"] = regs
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(data, indent=2) + "\n")
print("wrote", path, "insecure-registries", regs)
PY
}

install_nvidia_toolkit() {
  echo "installing NVIDIA Container Toolkit"
  apt_install ca-certificates curl gnupg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt_install nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  mkdir -p /etc/cdi
  nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml || true
  nvidia-ctk config --set nvidia-container-cli.no-cgroups --in-place || true
  start_docker
  service docker restart >/dev/null 2>&1 || true
}

resolve_agent_src() {
  if [ -n "$AGENT_SRC" ]; then
    echo "$AGENT_SRC"
    return
  fi
  local cand
  for cand in "$HERE/agent" /opt/lab-agent "$LAB_HOME/distributetr9_node" "$LAB_HOME/distributetr9_node/agent"; do
    if [ -f "$cand/agent.py" ]; then
      echo "$cand"
      return
    fi
  done
  echo "agent.py not found; pass --src /path/to/agent" >&2
  exit 1
}

if [ -f /etc/os-release ]; then
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ]; then
    echo "this installer supports Ubuntu (WSL). Detected: ${ID:-unknown}" >&2
    exit 1
  fi
fi

echo "lab user: $LAB_USER  home: $LAB_HOME"
if is_wsl; then
  echo "platform: WSL"
else
  echo "platform: native Linux (still no boot service)"
fi

if ! command -v python3 >/dev/null 2>&1; then
  apt_install python3
fi
require_cmd python3
require_cmd curl

install_docker_engine
require_cmd docker
groupadd -f docker
usermod -aG docker "$LAB_USER"
start_docker

SRC="$(resolve_agent_src)"
DEST=/opt/lab-agent
echo "copy agent $SRC -> $DEST"
install -d -m 755 -o "$LAB_USER" -g "$LAB_USER" "$DEST"
cp -R "$SRC"/. "$DEST"/
chown -R "$LAB_USER:$LAB_USER" "$DEST"
chmod +x "$DEST/run.sh" 2>/dev/null || true

install -d -m 755 /etc/lab-agent
cat > /etc/lab-agent/config.json <<EOF
{
  "api_base": "${API}",
  "executor": "${EXECUTOR}",
  "role": "${ROLE}"
}
EOF
chmod 644 /etc/lab-agent/config.json
chown root:root /etc/lab-agent /etc/lab-agent/config.json

install -d -m 700 -o "$LAB_USER" -g "$LAB_USER" /var/lab/attempts

if [ -n "$REGISTRY" ]; then
  merge_insecure_registry "$REGISTRY"
  service docker restart >/dev/null 2>&1 || start_docker
fi

if [ "$WANT_GPU" -eq 1 ]; then
  install_nvidia_toolkit
fi

cat > /usr/local/bin/lab-agent <<'EOF'
#!/usr/bin/env bash
exec bash /opt/lab-agent/run.sh "$@"
EOF
chmod 755 /usr/local/bin/lab-agent

echo
echo "=== verify ==="
if [ -f "$DEST/agent.py" ]; then
  grep -E '^AGENT_VERSION' "$DEST/agent.py" || true
fi
echo "config:"
cat /etc/lab-agent/config.json
if sudo -u "$LAB_USER" test -w /var/lab/attempts; then
  echo "attempts dir: writable by $LAB_USER"
else
  echo "attempts dir: NOT writable by $LAB_USER" >&2
  exit 1
fi
if sg docker -c "docker info" >/dev/null 2>&1; then
  echo "docker info: ok (via sg docker)"
else
  echo "docker info: failed in this session (expected until wsl --shutdown)"
  docker info >/dev/null 2>&1 && echo "docker info: ok as root" || echo "docker daemon not ready" >&2
fi
if curl -fsS -m 5 "${API%/}/health" >/dev/null; then
  echo "control API: reachable ${API%/}/health"
else
  echo "control API: not reachable yet (${API%/}/health) — check firewall / IP"
fi
if [ -n "$REGISTRY" ]; then
  if curl -fsS -m 5 "http://${REGISTRY}/v2/" >/dev/null; then
    echo "registry: reachable http://${REGISTRY}/v2/"
  else
    echo "registry: not reachable yet http://${REGISTRY}/v2/ — check insecure-registries / firewall"
  fi
fi

echo
echo "No boot service installed. Start when ready:"
echo "  lab-agent"
echo "  # or: bash /opt/lab-agent/run.sh"
if is_wsl; then
  echo
  echo "From Windows, once:  wsl --shutdown"
  echo "Then open a new WSL window so your user is in group docker."
  echo "Until then:  sg docker -c lab-agent"
fi
