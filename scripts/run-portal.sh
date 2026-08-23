#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/portal"
if [ ! -d node_modules ]; then
  npm install
fi
export CONTROL_API="${CONTROL_API:-http://127.0.0.1:8080}"
exec npm run dev
