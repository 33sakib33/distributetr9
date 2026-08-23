#!/usr/bin/env bash
# Mirror the catalog PyTorch image into the campus registry and print the
# RepoDigest to store in environments.image_digest.
# Usage: scripts/mirror-catalog.sh [host:port]
set -euo pipefail
REGISTRY="${1:-${REGISTRY_URL:-127.0.0.1:5001}}"
SRC="${PYTORCH_IMAGE:-pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime}"
DST="${REGISTRY}/lab/pytorch:2.5.1-cu124"

echo "registry $REGISTRY"
echo "pull $SRC"
docker pull "$SRC"
docker tag "$SRC" "$DST"
echo "push $DST"
docker push "$DST"
echo
echo "RepoDigest:"
docker image inspect --format '{{index .RepoDigests 0}}' "$DST"
echo
echo "On every lab Docker daemon, /etc/docker/daemon.json must include:"
echo "  {\"insecure-registries\": [\"${REGISTRY}\"]}"
echo "Then: sudo service docker restart"
echo
echo "Prove a second PC can: docker pull \$(docker image inspect --format '{{index .RepoDigests 0}}' $DST)"
echo "Custom requirements.txt stays off until REGISTRY_URL is set to this host:port."
