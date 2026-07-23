#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"
expected_image="nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40"

fail() {
  echo "runtime verification: $*" >&2
  echo "runtime verification: see README.md#5-常见故障" >&2
  exit 1
}

if [[ $(uname -s) != "Linux" ]]; then
  fail "unsupported operating system; expected Linux"
fi
if [[ $(uname -m) != "x86_64" ]]; then
  fail "unsupported architecture; expected x86_64/AMD64"
fi
command -v docker >/dev/null 2>&1 || fail "docker is not installed or not in PATH"
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"
command -v git >/dev/null 2>&1 || fail "git is not installed or not in PATH"

git diff --check || fail "git diff --check failed"
git fsck --full || fail "git fsck failed"
if [[ -n $(git status --porcelain) ]]; then
  fail "deployment checkout is not clean"
fi
bundle_ref=$("${repo_root}/scripts/validate-deployment-env.py" \
  --env-file "${repo_root}/.env" \
  --print-bundle-ref) || fail "root deployment environment is invalid"
[[ $(git rev-parse HEAD) == "${bundle_ref}" ]] || fail "deployed checkout HEAD does not match FLEET_BUNDLE_REF"
echo "runtime verification: fixed bundle ref, image digest and hashed dashboard auth are valid"

mapfile -t compose_images < <(docker compose config --images)
if [[ ${#compose_images[@]} -eq 0 ]]; then
  fail "Compose did not resolve a Hermes image"
fi
for image in "${compose_images[@]}"; do
  if [[ "${image}" != "${expected_image}" ]]; then
    fail "effective Compose image must equal the locked Hermes 0.19.0 AMD64 digest"
  fi
  docker image inspect "${image}" >/dev/null 2>&1 || fail "required local image is missing: ${image}"
done

"${repo_root}/scripts/verify-bundle.sh"

running_services=$(docker compose ps --status running --services)
if ! grep -qx "hermes" <<<"${running_services}"; then
  fail "hermes Compose service is not running"
fi
published_dashboard=$(docker compose port hermes 9119)
if [[ ! "${published_dashboard}" =~ ^127\.0\.0\.1:[1-9][0-9]*$ ]]; then
  fail "dashboard port must be published only on host loopback"
fi

container_id=$(docker compose ps -q hermes)
[[ -n "${container_id}" ]] || fail "could not resolve the running Hermes container"
expected_image_id=$(docker image inspect --format '{{.Id}}' "${expected_image}")
running_image_id=$(docker inspect --format '{{.Image}}' "${container_id}")
[[ "${running_image_id}" == "${expected_image_id}" ]] || fail "running container does not use the locked image digest"

if ! docker compose exec -T hermes /bin/bash -s <<'CONTAINER_CHECKS'
set -euo pipefail

profiles=(
  dispatcher prd-writer fde spec-writer spec-reviewer planner
  plan-reviewer tasker task-reviewer coder tester code-reviewer
)
gateway_profiles=(dispatcher prd-writer fde)

die() {
  echo "container runtime check: $*" >&2
  exit 1
}

is_gateway_profile() {
  local wanted=$1 profile
  for profile in "${gateway_profiles[@]}"; do
    [[ "${profile}" == "${wanted}" ]] && return 0
  done
  return 1
}

version=$(/opt/hermes/.venv/bin/python -c 'import hermes_cli; print(hermes_cli.__version__)')
[[ "${version}" == "0.19.0" ]] || die "expected Hermes 0.19.0, found ${version}"
echo "container runtime check: Hermes 0.19.0"

for profile in "${profiles[@]}"; do
  profile_root="/opt/data/profiles/${profile}"
  [[ -d "${profile_root}" ]] || die "profile is missing: ${profile}"
  for file in config.yaml distribution.yaml profile.yaml SOUL.md; do
    [[ -r "${profile_root}/${file}" ]] || die "${profile} is missing ${file}"
  done
  [[ -d "${profile_root}/skills" ]] || die "${profile} is missing skills"

  service="/run/service/gateway-${profile}"
  if is_gateway_profile "${profile}"; then
    [[ -e "${service}" ]] || die "s6 Gateway service slot is missing: ${profile}"
    up=$(/command/s6-svstat -o up "${service}")
    [[ "${up}" == "true" ]] || die "Gateway is not running: ${profile}"
  elif [[ -e "${service}" ]]; then
    up=$(/command/s6-svstat -o up "${service}")
    [[ "${up}" == "false" ]] || die "worker Gateway must be stopped: ${profile}"
  fi
done
echo "container runtime check: 12 profiles and 3 isolated Gateways"

/opt/hermes/.venv/bin/python - <<'PY_CONFIG'
import pathlib
import os
import yaml

profiles = (
    "dispatcher", "prd-writer", "fde", "spec-writer", "spec-reviewer", "planner",
    "plan-reviewer", "tasker", "task-reviewer", "coder", "tester", "code-reviewer",
)
fleet_model = os.environ.get("FLEET_MODEL", "")
if "/" not in fleet_model:
    raise SystemExit("FLEET_MODEL must use provider/model format")
expected_provider, expected_model = fleet_model.split("/", 1)
expected_base_url = os.environ.get("FLEET_MODEL_BASE_URL", "")
for profile in profiles:
    path = pathlib.Path("/opt/data/profiles") / profile / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    model = config.get("model")
    if not isinstance(model, dict):
        raise SystemExit(f"{profile}: model must be a provider/default mapping")
    if model.get("provider") != expected_provider:
        raise SystemExit(f"{profile}: model.provider must equal {expected_provider!r}")
    if model.get("default") != expected_model:
        raise SystemExit(f"{profile}: model.default must equal {expected_model!r}")
    if expected_base_url and model.get("base_url") != expected_base_url:
        raise SystemExit(f"{profile}: model.base_url must equal {expected_base_url!r}")
    if config.get("terminal", {}).get("home_mode") != "profile":
        raise SystemExit(f"{profile}: terminal.home_mode must equal profile")
    if config.get("memory", {}).get("write_approval") is not True:
        raise SystemExit(f"{profile}: memory.write_approval must be true")

dispatcher = yaml.safe_load(
    pathlib.Path("/opt/data/profiles/dispatcher/config.yaml").read_text(encoding="utf-8")
)
kanban = dispatcher.get("kanban", {})
expected = {
    "auto_decompose": False,
    "max_in_progress": 1,
    "max_in_progress_per_profile": 1,
    "orchestrator_profile": "dispatcher",
}
for key, value in expected.items():
    if kanban.get(key) != value:
        raise SystemExit(f"dispatcher: kanban.{key} must equal {value!r}")
PY_CONFIG
echo "container runtime check: fleet model endpoint, profile home, memory approval and Kanban policy"

dashboard_service=""
for candidate in /run/s6-rc/servicedirs/dashboard /run/service/dashboard; do
  if [[ -e "${candidate}" ]]; then
    dashboard_service="${candidate}"
    break
  fi
done
[[ -n "${dashboard_service}" ]] || die "Dashboard s6 service slot is missing"
dashboard_up=$(/command/s6-svstat -o up "${dashboard_service}")
[[ "${dashboard_up}" == "true" ]] || die "Dashboard is not running"

/opt/hermes/.venv/bin/python - <<'PY_DASHBOARD'
import json
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:9119/api/status", timeout=10) as response:
        payload = json.load(response)
except Exception as exc:
    raise SystemExit("Dashboard /api/status is unavailable") from exc
if payload.get("auth_required") is not True:
    raise SystemExit("Dashboard /api/status must report auth_required=true")
providers = payload.get("auth_providers", [])
if not isinstance(providers, list) or "basic" not in providers:
    raise SystemExit("Dashboard /api/status must advertise the basic auth provider")
PY_DASHBOARD
echo "container runtime check: Dashboard s6 service and basic authentication"

tool_handler=/opt/hermes/tools/kanban_tools.py
dispatcher_db=/opt/hermes/hermes_cli/kanban_db.py
kanban_cli=/opt/hermes/hermes_cli/kanban.py
dashboard_api=/opt/hermes/plugins/kanban/dashboard/plugin_api.py
completion_validator=/opt/fleet/scripts/validate_card_completion.py
completion_schema=/opt/fleet/schemas/card-completion.schema.json
grep -qF 'profile != "dispatcher"' "${tool_handler}" || die "worker Kanban guard is missing"
grep -qF '_require_dispatcher_profile("kanban_create")' "${tool_handler}" || die "kanban_create guard is missing"
grep -qF '_require_dispatcher_profile("kanban_link")' "${tool_handler}" || die "kanban_link guard is missing"
grep -qF "CompletionMetadataValidationError" "${tool_handler}" || die "Kanban completion tool guard is missing"
grep -qF "AND created_by = 'dispatcher'" "${dispatcher_db}" || die "dispatcher created_by guard is missing"
grep -qF "SDD_COMPLETION_SCHEMA_MARKER" "${dispatcher_db}" || die "formal completion kernel guard is missing"
grep -qF "kanban: completion blocked:" "${kanban_cli}" || die "Kanban CLI completion guard is missing"
grep -qF "status_code=422" "${dashboard_api}" || die "Dashboard completion guard is missing"
[[ -x "${completion_validator}" ]] || die "completion metadata validator is not executable"
validator_errors=""
if validator_errors=$(printf '{}\n' | /opt/hermes/.venv/bin/python \
    "${completion_validator}" \
    --schema "${completion_schema}" \
    --metadata-file - \
    --task-id t_runtime_check 2>&1); then
  die "completion metadata validator accepted an empty formal handoff"
fi
grep -qF '$.worktree: is required' <<<"${validator_errors}" || die "completion validator did not require worktree"
grep -qF '$.project_id: is required' <<<"${validator_errors}" || die "completion validator did not require project_id"
echo "container runtime check: dispatcher-only graph and formal completion guards"

read_lock_value() {
  local section=$1 key=$2
  awk -v wanted_section="${section}" -v wanted_key="${key}" '
    /^  [a-z0-9_]+:$/ {
      current = $1
      sub(/:$/, "", current)
    }
    current == wanted_section && $1 == wanted_key ":" {
      value = $2
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' /opt/fleet/config/skills-lock.yaml
}

glab_version=$(read_lock_value gitlab_official_skills cli_version)
gitlab_skills_rev=$(read_lock_value gitlab_official_skills revision)
lark_cli_version=$(read_lock_value lark_cli_official_skills cli_version)
lark_skills_rev=$(read_lock_value lark_cli_official_skills revision)
expected_lock="glab=${glab_version};gitlab=${gitlab_skills_rev};lark-cli=${lark_cli_version};lark=${lark_skills_rev};arch=amd64"
[[ -f /opt/fleet/vendor/current/.tooling-lock ]] || die "tooling lock material is missing"
[[ $(< /opt/fleet/vendor/current/.tooling-lock) == "${expected_lock}" ]] || die "tooling volume does not match skills-lock.yaml"
/opt/fleet/vendor/current/bin/glab --version >/dev/null || die "glab is not executable"
/opt/fleet/vendor/current/bin/lark-cli --version >/dev/null || die "lark-cli is not executable"
[[ -f /opt/fleet/vendor/current/gitlab/skills/glab/SKILL.md ]] || die "GitLab Skill is missing"
[[ -f /opt/fleet/vendor/current/lark/skills/lark-shared/SKILL.md ]] || die "lark-shared Skill is missing"
[[ -f /opt/fleet/vendor/current/lark/skills/lark-im/SKILL.md ]] || die "lark-im Skill is missing"
echo "container runtime check: locked glab, lark-cli and Skills"

/command/s6-setuidgid hermes python3 /opt/fleet/scripts/validate-profile-envs.py \
  --profiles-root /opt/data/profiles \
  --owner hermes \
  --runtime-check \
  --glab-bin /opt/fleet/vendor/current/bin/glab
CONTAINER_CHECKS
then
  fail "container, Gateway, tooling or identity verification failed"
fi

echo "runtime verification: deployable read-only runtime checks passed"
echo "runtime verification: production autonomous E2E still requires the README smoke/integration tests"
