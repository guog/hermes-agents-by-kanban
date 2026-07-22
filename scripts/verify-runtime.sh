#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "${repo_root}"

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

mapfile -t compose_images < <(docker compose config --images)
if [[ ${#compose_images[@]} -eq 0 ]]; then
  fail "Compose did not resolve a Hermes image"
fi
for image in "${compose_images[@]}"; do
  if [[ "${image}" != "nousresearch/hermes-agent:v2026.7.20" ]]; then
    fail "effective Compose image must be nousresearch/hermes-agent:v2026.7.20"
  fi
  docker image inspect "${image}" >/dev/null 2>&1 || fail "required local image is missing: ${image}"
done

"${repo_root}/scripts/verify-bundle.sh"

running_services=$(docker compose ps --status running --services)
if ! grep -qx "hermes" <<<"${running_services}"; then
  fail "hermes Compose service is not running"
fi

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

tool_handler=/opt/hermes/tools/kanban_tools.py
dispatcher_db=/opt/hermes/hermes_cli/kanban_db.py
grep -qF 'profile != "dispatcher"' "${tool_handler}" || die "worker Kanban guard is missing"
grep -qF '_require_dispatcher_profile("kanban_create")' "${tool_handler}" || die "kanban_create guard is missing"
grep -qF '_require_dispatcher_profile("kanban_link")' "${tool_handler}" || die "kanban_link guard is missing"
grep -qF "AND created_by = 'dispatcher'" "${dispatcher_db}" || die "dispatcher created_by guard is missing"
echo "container runtime check: dispatcher-only Kanban guard"

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
