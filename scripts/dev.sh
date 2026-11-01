#!/usr/bin/env bash
# One-command local stack: Postgres, MinIO, registry, control API, portal, and
# a train + build agent on this machine. Ctrl-C stops everything it started.
#
#   scripts/dev.sh              # everything
#   DEV_PORTAL=0 scripts/dev.sh # skip the portal
#   DEV_AGENTS=0 scripts/dev.sh # skip the local agents
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DEV_DIR="$ROOT/.dev"
LOGS="$DEV_DIR/logs"
mkdir -p "$LOGS"

export REGISTRY_URL="${REGISTRY_URL:-127.0.0.1:5001}"
export LAB_API="${LAB_API:-http://127.0.0.1:8080}"
unset LAB_IMAGE

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  echo "stopping dev stack"
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

start() {
  local name="$1"
  shift
  "$@" >"$LOGS/$name.log" 2>&1 &
  pids+=("$!")
  echo "started $name (pid $!, log .dev/logs/$name.log)"
}

echo "compose: postgres minio registry"
docker compose up -d --wait postgres minio registry

start api ./scripts/run-control-lan.sh
for _ in $(seq 1 60); do
  curl -sf "$LAB_API/health" >/dev/null && break
  sleep 1
done
curl -sf "$LAB_API/health" >/dev/null || { echo "API did not come up; see .dev/logs/api.log"; exit 1; }
echo "api ready at $LAB_API (registry $REGISTRY_URL)"

if [ "${DEV_PORTAL:-1}" = "1" ]; then
  start portal ./scripts/run-portal.sh
  echo "portal at http://127.0.0.1:3000"
fi

if [ "${DEV_AGENTS:-1}" = "1" ]; then
  for role in train build; do
    mkdir -p "$DEV_DIR/attempts-$role"
    start "agent-$role" python3 -u agent/agent.py \
      --api "$LAB_API" --role "$role" --executor "${DEV_EXECUTOR:-auto}" \
      --state "$DEV_DIR/agent-$role.json" --attempt-root "$DEV_DIR/attempts-$role"
  done
fi

echo
echo "dev stack up. Submit a job: python3 scripts/smoke.py   (Ctrl-C to stop)"
tail -n 0 -F "$LOGS"/*.log
