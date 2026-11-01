#!/usr/bin/env bash
# Restore a backup made by deploy/backup.sh. Stops the API while it runs.
#
#   deploy/restore.sh backups/20261101-031500
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="$(cd "${1:?usage: deploy/restore.sh BACKUP_DIR}" && pwd)"
ENV_FILE="${ENV_FILE:-deploy/prod.env}"
COMPOSE="${COMPOSE:-docker compose -f deploy/docker-compose.prod.yml --env-file $ENV_FILE}"
NETWORK="${NETWORK:-lab-prod_default}"
PG_DB="${PG_DB:-lab}"
BUCKET="${BUCKET:-lab}"
MINIO_IMAGE="${MINIO_IMAGE:-cgr.dev/chainguard/minio:latest@sha256:3b6e40c30ecd5cea627e382ff0b82ca841f58c52294578787043ee80c8ef7c82}"
STOP_CONTROL="${STOP_CONTROL:-1}"
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

[ "$STOP_CONTROL" = "1" ] && $COMPOSE stop control

echo "postgres <- $SRC/postgres.dump"
$COMPOSE exec -T postgres pg_restore -U lab -d "$PG_DB" --clean --if-exists --no-owner < "$SRC/postgres.dump"

echo "minio bucket $BUCKET <- $SRC/minio/"
docker run --rm --network "$NETWORK" --user 0:0 --entrypoint sh \
  -e MC_HOST_lab="http://${MINIO_ROOT_USER}:${MINIO_ROOT_PASSWORD}@minio:9000" \
  -v "$SRC:/backup:ro" "$MINIO_IMAGE" -c "mc mb --ignore-existing lab/$BUCKET && mc mirror --quiet --overwrite /backup/minio lab/$BUCKET"

if [ -f "$SRC/agent-signing-key.pem" ] && [ "$STOP_CONTROL" = "1" ]; then
  echo "agent signing key <- $SRC/agent-signing-key.pem"
  $COMPOSE run --rm -T --no-deps --entrypoint sh control -c 'cat > /data/agent-signing-key.pem && chmod 600 /data/agent-signing-key.pem' < "$SRC/agent-signing-key.pem"
fi

[ "$STOP_CONTROL" = "1" ] && $COMPOSE start control
echo "restored from $SRC"
