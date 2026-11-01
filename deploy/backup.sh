#!/usr/bin/env bash
# Nightly backup of the lab server: Postgres, MinIO (job inputs, logs, outputs)
# and the agent signing key. Registry images are not backed up; they rebuild.
#
#   deploy/backup.sh [DEST_ROOT]          # default ./backups, keeps KEEP_DAYS (14)
#   cron: 15 3 * * *  cd /srv/lab && deploy/backup.sh /srv/backups >> /var/log/lab-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE="${ENV_FILE:-deploy/prod.env}"
COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.prod.yml --env-file $ENV_FILE}"
NETWORK="${NETWORK:-lab-prod_default}"
PG_DB="${PG_DB:-lab}"
BUCKET="${BUCKET:-lab}"
KEEP_DAYS="${KEEP_DAYS:-14}"
MINIO_IMAGE="${MINIO_IMAGE:-cgr.dev/chainguard/minio:latest@sha256:3b6e40c30ecd5cea627e382ff0b82ca841f58c52294578787043ee80c8ef7c82}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

ROOT="${1:-backups}"
DEST="$ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd)"

echo "postgres -> $DEST/postgres.dump"
$COMPOSE exec -T postgres pg_dump -U lab -Fc "$PG_DB" > "$DEST/postgres.dump"

echo "minio bucket $BUCKET -> $DEST/minio/"
docker run --rm --network "$NETWORK" --user 0:0 --entrypoint mc \
  -e MC_HOST_lab="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
  -v "$DEST:/backup" "$MINIO_IMAGE" mirror --quiet --overwrite "lab/$BUCKET" /backup/minio

if $COMPOSE exec -T control test -f /data/agent-signing-key.pem 2>/dev/null; then
  echo "agent signing key -> $DEST/agent-signing-key.pem"
  $COMPOSE exec -T control cat /data/agent-signing-key.pem > "$DEST/agent-signing-key.pem"
  chmod 600 "$DEST/agent-signing-key.pem"
fi

du -sh "$DEST" | awk '{print "backup size " $1}'
find "$ROOT" -mindepth 1 -maxdepth 1 -type d -mtime +"$KEEP_DAYS" -print -exec rm -rf {} +
echo "done: $DEST"
