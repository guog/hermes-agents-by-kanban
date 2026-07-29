#!/command/with-contenv sh
set -eu

PYTHON=/opt/hermes/.venv/bin/python
UV=/usr/local/bin/uv
CACHE_DIR=/opt/data/.cache/uv
INDEX_URL=${UV_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple/}

log() {
  printf '%s\n' "[container-init] $*"
}

has_feishu_runtime() {
  "$PYTHON" - <<'PY'
from importlib.metadata import PackageNotFoundError, version

expected = {
    "lark-oapi": "1.6.8",
    "qrcode": "7.4.2",
    "requests-toolbelt": "1.0.0",
}
for package, required in expected.items():
    try:
        installed = version(package)
    except PackageNotFoundError:
        raise SystemExit(1)
    if installed != required:
        raise SystemExit(1)

import lark_oapi  # noqa: F401
PY
}

if has_feishu_runtime; then
  log "Hermes Feishu adapter dependencies already match; installation skipped"
  exit 0
fi

if [ ! -x "$PYTHON" ] || [ ! -x "$UV" ]; then
  log "ERROR: managed Python or uv is unavailable"
  exit 1
fi
if [ ! -d /opt/data ] || [ ! -w /opt/data ]; then
  log "ERROR: /opt/data is not a writable persistent runtime directory"
  exit 1
fi

mkdir -p "$CACHE_DIR"
log "installing Hermes 0.19.0 Feishu adapter dependencies"
"$UV" --no-config pip install \
  --python "$PYTHON" \
  --cache-dir "$CACHE_DIR" \
  --index-url "$INDEX_URL" \
  "lark-oapi==1.6.8" \
  "qrcode==7.4.2" \
  "requests-toolbelt==1.0.0"

if ! has_feishu_runtime; then
  log "ERROR: Feishu adapter dependency verification failed"
  exit 1
fi

log "Hermes Feishu adapter dependencies installed and verified"
