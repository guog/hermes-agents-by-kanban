#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <board-slug> <group/project> <checkout-path-inside-container>" >&2
  exit 2
fi

board_slug=$1
project_path=$2
checkout_path=$3

if [[ ! "${board_slug}" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
  echo "board slug must be lowercase kebab-case" >&2
  exit 2
fi
if [[ "${checkout_path}" != /* ]]; then
  echo "checkout path must be an absolute path inside the container" >&2
  exit 2
fi
if ! docker compose exec -T hermes test -d "${checkout_path}"; then
  echo "checkout path does not exist inside the hermes container: ${checkout_path}" >&2
  exit 1
fi

docker compose exec -T hermes hermes -p dispatcher kanban boards create "${board_slug}" \
  --name "SDD - ${project_path}" \
  --description "Serial PRD-to-code delivery for ${project_path}" \
  --default-workdir "${checkout_path}"
