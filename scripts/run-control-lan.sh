#!/usr/bin/env bash
# Run the control API on the Mac host so LAN desktops can connect.
# Docker Desktop published ports are often blocked by the macOS firewall
# ("No route to host"). Host Python is already allowed incoming.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Load .env if present (REGISTRY_URL, etc.) without overriding an explicit export.
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

LAN_HOST="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://lab:lab@127.0.0.1:5433/lab}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-127.0.0.1:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-labminio}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-labminio}"
export MINIO_BUCKET="${MINIO_BUCKET:-lab}"
export MINIO_SECURE="${MINIO_SECURE:-false}"
export DEV_AUTH="${DEV_AUTH:-1}"
export DEV_AUTH_ADMIN="${DEV_AUTH_ADMIN:-1}"
export PUBLIC_BASE_URL="http://${LAN_HOST}:8080"
export TOKEN_PEPPER="${TOKEN_PEPPER:-dev-token-pepper-change-me}"
export PORTAL_BASE_URL="${PORTAL_BASE_URL:-http://127.0.0.1:3000}"
export REGISTRY_URL="${REGISTRY_URL:-}"
export OIDC_DEV="${OIDC_DEV:-1}"
export OIDC_ISSUER="${OIDC_ISSUER:-http://127.0.0.1:8080/oidc}"
export OIDC_REDIRECT_URI="${OIDC_REDIRECT_URI:-http://127.0.0.1:8080/auth/callback}"

echo "LAN API: $PUBLIC_BASE_URL"
echo "REGISTRY_URL: ${REGISTRY_URL:-unset (catalog images only)}"
echo "Keep postgres+minio+registry in Docker: docker compose up -d postgres minio registry"
exec "$ROOT/control/.venv/bin/uvicorn" app.main:app \
  --app-dir "$ROOT/control" \
  --host 0.0.0.0 \
  --port 8080 \
  --reload
