#!/bin/sh
set -eu

target_uid="${HERMES_UID:-${PUID:-1000}}"
target_gid="${HERMES_GID:-${PGID:-1000}}"

validate_id() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) [ "$1" -ge 1 ] && [ "$1" -le 65534 ] ;;
  esac
}

if ! validate_id "$target_uid" || ! validate_id "$target_gid"; then
  echo "controller-entrypoint: invalid Hermes UID/GID" >&2
  exit 64
fi

if [ "$(id -u hermes)" != "$target_uid" ]; then
  usermod -u "$target_uid" hermes
fi
if [ "$(id -g hermes)" != "$target_gid" ]; then
  groupmod -o -g "$target_gid" hermes
fi

exec /opt/hermes/.venv/bin/python -m hollysys_controller.daemon
