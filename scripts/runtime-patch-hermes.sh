#!/usr/bin/env bash
set -euo pipefail

install_root=/opt/hermes
patch_file=/opt/fleet/patches/hermes-0.19.0-dispatcher-kanban-guard.patch
python_bin="${install_root}/.venv/bin/python"
tool_handler="${install_root}/tools/kanban_tools.py"
dispatcher_db="${install_root}/hermes_cli/kanban_db.py"
kanban_cli="${install_root}/hermes_cli/kanban.py"
dashboard_api="${install_root}/plugins/kanban/dashboard/plugin_api.py"
completion_validator=/opt/fleet/scripts/validate_card_completion.py
completion_schema=/opt/fleet/schemas/card-completion.schema.json

test -r "${patch_file}"
test -r "${completion_validator}"
test -r "${completion_schema}"
test -x "${python_bin}"
test -w "${tool_handler}"
test -w "${dispatcher_db}"
test -w "${kanban_cli}"
test -w "${dashboard_api}"

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

"${python_bin}" - \
  "${tool_handler}" "${dispatcher_db}" "${kanban_cli}" \
  "${dashboard_api}" "${completion_validator}" "${completion_schema}" <<'PY'
import importlib.util
import pathlib
import sys

tool_source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
dispatcher_source = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")
cli_source = pathlib.Path(sys.argv[3]).read_text(encoding="utf-8")
dashboard_source = pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")
validator_path = pathlib.Path(sys.argv[5])
schema_path = pathlib.Path(sys.argv[6])
tool_required = (
    'profile != "dispatcher"',
    '_require_dispatcher_profile("kanban_create")',
    '_require_dispatcher_profile("kanban_link")',
    "CompletionMetadataValidationError",
    "one flat",
)
dispatcher_required = (
    "AND created_by = 'dispatcher'",
    "status = 'ready'",
    "status = 'review'",
    "SDD_COMPLETION_SCHEMA_MARKER",
    "_validate_formal_sdd_completion(conn, task_id, metadata)",
    "edit_completed_task_result",
)
cli_required = (
    "kanban: completion blocked:",
    "kanban: edit blocked:",
)
dashboard_required = (
    "CompletionMetadataValidationError",
    "status_code=422",
)
missing = [value for value in tool_required if value not in tool_source]
missing += [value for value in dispatcher_required if value not in dispatcher_source]
missing += [value for value in cli_required if value not in cli_source]
missing += [value for value in dashboard_required if value not in dashboard_source]
if missing:
    raise SystemExit(f"fleet runtime patch verification failed: {missing}")

spec = importlib.util.spec_from_file_location(
    "fleet_completion_validator",
    validator_path,
)
if spec is None or spec.loader is None:
    raise SystemExit("fleet completion validator cannot be loaded")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
errors = validator.validate_completion_metadata(
    {},
    task_id="t_runtime_check",
    schema_path=schema_path,
)
for field in ("$.worktree: is required", "$.project_id: is required"):
    if field not in errors:
        raise SystemExit(
            f"fleet completion validator smoke check missing: {field}"
        )
PY
