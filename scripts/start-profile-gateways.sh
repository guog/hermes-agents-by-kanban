#!/command/with-contenv bash
set -euo pipefail

start_gateway() {
  local profile=$1 attempt

  for attempt in 1 2 3 4 5; do
    if /command/s6-setuidgid hermes hermes -p "${profile}" gateway status >/dev/null 2>&1; then
      return 0
    fi
    if /command/s6-setuidgid hermes hermes -p "${profile}" gateway start >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "fleet gateways: ${profile} did not reach running state" >&2
  return 1
}

for profile in dispatcher prd-writer fde; do
  start_gateway "${profile}"
done

echo "fleet gateways: dispatcher, prd-writer and fde requested"
