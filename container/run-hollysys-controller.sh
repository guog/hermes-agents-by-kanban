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

if ! command -v setpriv >/dev/null 2>&1; then
  echo "controller-entrypoint: setpriv is required" >&2
  exit 69
fi

controller_home=/opt/data/controller-home
install -d -o "$target_uid" -g "$target_gid" -m 0750 \
  "$controller_home" \
  /opt/data/scratch \
  /run/hollysys-controller \
  /var/lib/hollysys-controller \
  /workspace/projects

export HOME="$controller_home"

setpriv --reuid=hermes --regid=hermes --init-groups \
  /opt/hermes/.venv/bin/python \
  /opt/fleet/container/sync-lark-config.py

exec setpriv --reuid=hermes --regid=hermes --init-groups \
  /opt/hermes/.venv/bin/python -m hollysys_controller.daemon
