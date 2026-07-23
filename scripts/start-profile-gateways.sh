#!/command/with-contenv bash
set -euo pipefail

for profile in dispatcher prd-writer fde; do
  /command/s6-setuidgid hermes hermes -p "${profile}" gateway start >/dev/null
done

echo "fleet gateways: dispatcher, prd-writer and fde requested"
