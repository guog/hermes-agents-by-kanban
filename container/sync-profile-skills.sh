#!/command/with-contenv sh
set -eu

exec s6-setuidgid hermes \
  /opt/hermes/.venv/bin/python \
  /opt/fleet/container/sync-profile-skills.py
