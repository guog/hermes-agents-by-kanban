#!/usr/bin/env bash
set -euo pipefail

install_root=/opt/hermes
patch_file=/opt/fleet/patches/hermes-0.19.0-dispatcher-kanban-guard.patch
python_bin="${install_root}/.venv/bin/python"
tool_handler="${install_root}/tools/kanban_tools.py"
dispatcher_db="${install_root}/hermes_cli/kanban_db.py"

test -r "${patch_file}"
test -x "${python_bin}"
test -w "${tool_handler}"
test -w "${dispatcher_db}"

version=$("${python_bin}" -c 'import hermes_cli; print(hermes_cli.__version__)')
if [[ "${version}" != "0.19.0" ]]; then
  echo "fleet runtime patch: expected Hermes 0.19.0, found ${version}" >&2
  exit 66
fi

if git -C "${install_root}" apply --reverse --check "${patch_file}" >/dev/null 2>&1; then
  echo "fleet runtime patch: dispatcher Kanban guard already applied"
else
  git -C "${install_root}" apply --check "${patch_file}"
  git -C "${install_root}" apply "${patch_file}"
  echo "fleet runtime patch: dispatcher Kanban guard applied"
fi

"${python_bin}" - "${tool_handler}" "${dispatcher_db}" <<'PY'
import pathlib
import sys

tool_source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
dispatcher_source = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
tool_required = (
    'profile != "dispatcher"',
    '_require_dispatcher_profile("kanban_create")',
    '_require_dispatcher_profile("kanban_link")',
)
dispatcher_required = (
    "AND created_by = 'dispatcher'",
    "status = 'ready'",
    "status = 'review'",
)
missing = [value for value in tool_required if value not in tool_source]
missing += [value for value in dispatcher_required if value not in dispatcher_source]
if missing:
    raise SystemExit(f"fleet runtime patch verification failed: {missing}")
PY
