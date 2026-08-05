#!/bin/sh
set -eu

image=${1:-hollysys-hermes-agents:latest}
suffix=$$
hermes_container="hollysys-supervisor-hermes-${suffix}"
controller_container="hollysys-supervisor-controller-${suffix}"
socket_volume="hollysys-supervisor-socket-${suffix}"
data_volume="hollysys-supervisor-data-${suffix}"

cleanup() {
  docker rm -f "$controller_container" "$hermes_container" >/dev/null 2>&1 || true
  docker volume rm "$socket_volume" "$data_volume" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

docker image inspect "$image" >/dev/null
docker volume create "$socket_volume" >/dev/null
docker volume create "$data_volume" >/dev/null

docker run -d \
  --name "$hermes_container" \
  -e HERMES_HOME=/opt/data \
  -e HOLLYSYS_WORKER_SUPERVISOR_SOCKET=/run/hollysys-controller/worker-supervisor.sock \
  -v "$socket_volume:/run/hollysys-controller" \
  -v "$data_volume:/opt/data" \
  "$image" sleep infinity >/dev/null

docker run -d \
  --name "$controller_container" \
  --user hermes \
  --entrypoint sleep \
  -v "$socket_volume:/run/hollysys-controller" \
  "$image" infinity >/dev/null

attempt=0
while [ "$attempt" -lt 30 ]; do
  if docker exec --user hermes "$controller_container" \
    test -S /run/hollysys-controller/worker-supervisor.sock; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
if [ "$attempt" -eq 30 ]; then
  echo "worker supervisor socket was not ready" >&2
  exit 1
fi

hermes_pid_namespace=$(docker exec "$hermes_container" readlink /proc/1/ns/pid)
controller_pid_namespace=$(docker exec "$controller_container" readlink /proc/1/ns/pid)
if [ "$hermes_pid_namespace" = "$controller_pid_namespace" ]; then
  echo "containers unexpectedly share a PID namespace" >&2
  exit 1
fi

docker exec --user hermes "$hermes_container" sh -c '
  HERMES_KANBAN_BOARD=default \
  HERMES_KANBAN_TASK=supervisor-integration \
  HERMES_KANBAN_RUN_ID=17 \
  setsid sh -c "sleep 300 & wait" >/opt/data/worker.log 2>&1 &
  echo "$!" >/opt/data/worker.pid
'
worker_pid=$(docker exec "$hermes_container" sh -c 'cat /opt/data/worker.pid')

docker exec -i --user hermes "$hermes_container" \
  python - "$worker_pid" <<'PY'
import sqlite3
import sys

worker_pid = int(sys.argv[1])
with sqlite3.connect("/opt/data/kanban.db") as connection:
    connection.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_by TEXT NOT NULL,
            current_run_id INTEGER,
            worker_pid INTEGER
        )
        """
    )
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, ?, ?, ?)",
        (
            "supervisor-integration",
            "running",
            "hollysys-controller",
            17,
            worker_pid,
        ),
    )
PY

docker exec -i --user hermes "$controller_container" \
  python - "$worker_pid" <<'PY'
import sys
from pathlib import Path

from hollysys_controller.worker_recovery import (
    UnixWorkerSupervisorClient,
    WorkerIdentity,
)

worker_pid = int(sys.argv[1])
identity = WorkerIdentity("default", "supervisor-integration", 17, worker_pid)
client = UnixWorkerSupervisorClient(
    Path("/run/hollysys-controller/worker-supervisor.sock")
)
probe = client.probe(identity)
assert probe.state == "running", probe
assert probe.process_count >= 2, probe
terminated = client.terminate(identity)
assert terminated.state == "terminated", terminated
assert terminated.process_count >= 2, terminated
print(
    "supervisor integration passed: "
    f"worker_pid={worker_pid} processes={terminated.process_count} "
    f"signal={terminated.signal}"
)
PY

if docker exec "$hermes_container" \
  sh -c "test -e /proc/$worker_pid/stat && ! grep -q ') Z ' /proc/$worker_pid/stat"; then
  echo "worker still exists after confirmed termination" >&2
  exit 1
fi

echo "independent PID namespaces passed: hermes=$hermes_pid_namespace controller=$controller_pid_namespace"
