#!/usr/bin/env bash
# One-shot lab node install inside WSL Ubuntu (or Debian).
# Installs Docker Engine, writes /etc and /var with readable perms, copies the
# agent. Does NOT install a boot service — start the agent yourself.
#
# Usage (from the repo, inside WSL):
#   sudo bash scripts/setup-lab-node.sh --api http://10.10.126.226:8080
#   sudo bash scripts/setup-lab-node.sh --api http://10.10.126.226:8080 --registry 10.10.126.226:5001
#   sudo bash scripts/setup-lab-node.sh --api http://HOST:8080 --role build --gpu
#   sudo bash scripts/setup-lab-node.sh --api http://HOST:8080 --room "Lab 204"
set -euo pipefail

API=""
REGISTRY=""
ROLE="train"
ROOM=""
EXECUTOR="docker"
WANT_GPU=0
AGENT_SRC=""
RESET_IDENTITY=0
CA_CERT=""

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  echo "Flags: --api URL  --registry HOST:PORT  --role train|build  --executor docker|auto"
  echo "       --room NAME  --src DIR  --gpu  --reset-identity  -h"
  echo "  --room NAME       the lab room this PC is in (shown in the portal, kept on re-runs)"
  echo "  --reset-identity  forget this PC's node id (use before imaging a disk)"
  echo "  --ca-cert FILE    trust the lab server's CA (deploy/ with LAB_TLS=internal)"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --api) API="${2:-}"; shift 2 ;;
    --registry) REGISTRY="${2:-}"; shift 2 ;;
    --role) ROLE="${2:-}"; shift 2 ;;
    --room) ROOM="${2:-}"; shift 2 ;;
    --executor) EXECUTOR="${2:-}"; shift 2 ;;
    --src) AGENT_SRC="${2:-}"; shift 2 ;;
    --gpu) WANT_GPU=1; shift ;;
    --reset-identity) RESET_IDENTITY=1; shift ;;
    --ca-cert) CA_CERT="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown flag: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ -z "$API" ]; then
  echo "missing --api http://<control-host>:8080" >&2
  exit 2
fi
case "$ROLE" in train|build) ;; *) echo "role must be train or build" >&2; exit 2 ;; esac
# The room goes into config.json by hand, so keep it to characters that need no JSON escaping.
if [ -n "$ROOM" ] && ! printf '%s' "$ROOM" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$'; then
  echo "room must be 1-64 letters, digits, spaces, dots, dashes or underscores" >&2
  exit 2
fi
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

# Docker must come up after every WSL boot with nobody logged in. With systemd
# the docker unit does that; run.sh's `sudo -n service docker start` is the
# fallback, allowed for exactly that one command.
if is_wsl; then
  touch /etc/wsl.conf
  if ! grep -q '^\[boot\]' /etc/wsl.conf; then
    printf '\n[boot]\nsystemd=true\n' >> /etc/wsl.conf
    echo "enabled systemd in /etc/wsl.conf (takes effect after wsl --shutdown)"
  fi
fi
SERVICE_BIN="$(command -v service || echo /usr/sbin/service)"
echo "$LAB_USER ALL=(root) NOPASSWD: $SERVICE_BIN docker start" > /etc/sudoers.d/lab-agent
chmod 440 /etc/sudoers.d/lab-agent
if command -v visudo >/dev/null 2>&1 && ! visudo -cf /etc/sudoers.d/lab-agent >/dev/null; then
  rm -f /etc/sudoers.d/lab-agent
  echo "warning: could not add the docker-start sudo rule" >&2
fi

SRC="$(resolve_agent_src)"
DEST=/opt/lab-agent
VERSION="$(sed -n 's/^AGENT_VERSION = "\(.*\)"/\1/p' "$SRC/agent.py")"
[ -n "$VERSION" ] || { echo "cannot read AGENT_VERSION from $SRC/agent.py" >&2; exit 1; }
# Release layout for self-update: releases/<version>, current -> that release.
echo "install agent $VERSION: $SRC -> $DEST/releases/$VERSION"
install -d -m 755 -o "$LAB_USER" -g "$LAB_USER" "$DEST" "$DEST/releases"
rm -rf "$DEST/releases/$VERSION"
mkdir -p "$DEST/releases/$VERSION"
cp "$SRC"/*.py "$SRC"/*.json "$SRC"/*.sh "$DEST/releases/$VERSION"/ 2>/dev/null || true
ln -sfn "releases/$VERSION" "$DEST/current"
cp "$SRC/launcher.sh" "$DEST/launcher.sh"
# Files from the old flat layout would shadow the release in older scripts.
find "$DEST" -maxdepth 1 -type f \( -name '*.py' -o -name '*.json' -o -name 'run.sh' \) -delete
rm -f "$DEST/pending" "$DEST/pending.fails" "$DEST/rejected"
chown -R "$LAB_USER:$LAB_USER" "$DEST"
chmod +x "$DEST/launcher.sh" "$DEST/releases/$VERSION"/*.sh

if [ "$RESET_IDENTITY" -eq 1 ]; then
  rm -f "$LAB_HOME/.distributetr9/agent-state.json"
  echo "node identity reset; this PC registers as a new node on next start"
fi

if [ -n "$CA_CERT" ]; then
  # Caddy's internal CA: trusted by the agent (system store) and by Docker for the registry.
  install -m 644 "$CA_CERT" /usr/local/share/ca-certificates/lab-ca.crt
  update-ca-certificates >/dev/null
  if [ -n "$REGISTRY" ]; then
    install -d /etc/docker/certs.d/"$REGISTRY"
    install -m 644 "$CA_CERT" /etc/docker/certs.d/"$REGISTRY"/ca.crt
  fi
  echo "trusted lab CA from $CA_CERT"
fi

# Pin the update-signing key now, over the network the admin is watching.
BUNDLE_KEY="$(curl -fsS -m 10 "${API%/}/agent/bundle-key" 2>/dev/null || true)"
if [ -z "$BUNDLE_KEY" ]; then
  echo "warning: could not fetch ${API%/}/agent/bundle-key; the agent will pin the key on first register"
  BUNDLE_KEY="null"
fi

install -d -m 755 /etc/lab-agent
CONFIG_JSON=/etc/lab-agent/config.json
# Re-running without --room keeps the room the last install set.
if [ -z "$ROOM" ] && [ -f "$CONFIG_JSON" ]; then
  ROOM="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1])).get("room") or "")' "$CONFIG_JSON" 2>/dev/null || true)"
  printf '%s' "$ROOM" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$' || ROOM=""
fi
ROOM_LINE=""
if [ -n "$ROOM" ]; then ROOM_LINE="  \"room\": \"${ROOM}\","$'\n'; fi
cat > "$CONFIG_JSON" <<EOF
{
  "api_base": "${API}",
  "executor": "${EXECUTOR}",
  "role": "${ROLE}",
${ROOM_LINE}  "bundle_public_key": ${BUNDLE_KEY}
}
EOF
chmod 644 /etc/lab-agent/config.json
chown root:root /etc/lab-agent /etc/lab-agent/config.json

install -d -m 700 -o "$LAB_USER" -g "$LAB_USER" /var/lab/attempts

if [ -n "$REGISTRY" ] && [ -z "$CA_CERT" ]; then
  # Plain-HTTP dev registry. With --ca-cert the registry is HTTPS and trusted instead.
  merge_insecure_registry "$REGISTRY"
  service docker restart >/dev/null 2>&1 || start_docker
fi

if [ "$WANT_GPU" -eq 1 ]; then
  install_nvidia_toolkit
fi

cat > /usr/local/bin/lab-agent <<'EOF'
#!/usr/bin/env bash
exec bash /opt/lab-agent/launcher.sh "$@"
EOF
chmod 755 /usr/local/bin/lab-agent

echo
echo "=== verify ==="
echo "agent: $(readlink "$DEST/current")"
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
  SCHEME=http
  [ -n "$CA_CERT" ] && SCHEME=https
  if curl -fsS -m 5 "${SCHEME}://${REGISTRY}/v2/" >/dev/null; then
    echo "registry: reachable ${SCHEME}://${REGISTRY}/v2/"
  else
    echo "registry: not reachable yet ${SCHEME}://${REGISTRY}/v2/ — check certs / insecure-registries / firewall"
  fi
fi

echo
echo "No boot service installed. Start when ready:"
echo "  lab-agent"
echo "  # updates itself from the control API; rolls back a release that will not start"
if is_wsl; then
  echo
  echo "From Windows, once:  wsl --shutdown"
  echo "Then open a new WSL window so your user is in group docker."
  echo "Until then:  sg docker -c lab-agent"
fi
