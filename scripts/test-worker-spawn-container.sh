#!/usr/bin/env bash
set -euo pipefail

if (($# != 1)); then
  printf 'usage: %s IMAGE\n' "$0" >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="$1"

docker run --rm \
  --platform linux/amd64 \
  --entrypoint /opt/hermes/.venv/bin/python \
  --mount \
  "type=bind,src=${SCRIPT_DIR}/test-worker-spawn-contract.py,dst=/tmp/test-worker-spawn-contract.py,readonly" \
  "${IMAGE}" \
  /tmp/test-worker-spawn-contract.py
