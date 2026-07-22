#!/usr/bin/env bash
set -euo pipefail
umask 077

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
profiles=(
  dispatcher prd-writer fde spec-writer spec-reviewer planner
  plan-reviewer tasker task-reviewer coder tester code-reviewer
)

root_env_value() {
  local key=$1 env_file="${repo_root}/.env" line value
  [[ -f "${env_file}" ]] || return 0
  line=$(grep -E "^[[:space:]]*${key}=" "${env_file}" | tail -n 1 || true)
  [[ -n "${line}" ]] || return 0
  value=${line#*=}
  value=${value#"${value%%[![:space:]]*}"}
  value=${value%"${value##*[![:space:]]}"}
  if [[ ${#value} -ge 2 && (
        ( "${value:0:1}" == '"' && "${value: -1}" == '"' ) ||
        ( "${value:0:1}" == "'" && "${value: -1}" == "'" )
      ) ]]; then
    value=${value:1:${#value}-2}
  fi
  printf '%s' "${value}"
}

data_dir=${HERMES_DATA_DIR:-}
if [[ -z "${data_dir}" ]]; then
  data_dir=$(root_env_value HERMES_DATA_DIR)
fi
data_dir=${data_dir:-./.runtime/hermes}
if [[ "${data_dir}" != /* ]]; then
  data_dir="${repo_root}/${data_dir#./}"
fi

profiles_root="${data_dir}/profiles"
install -d -m 0700 "${data_dir}" "${profiles_root}"

created=0
existing=0
for profile in "${profiles[@]}"; do
  source_env="${repo_root}/profiles/${profile}/.env.template"
  profile_dir="${profiles_root}/${profile}"
  target_env="${profile_dir}/.env"

  install -d -m 0700 "${profile_dir}"
  if [[ -L "${target_env}" ]]; then
    echo "profile env init: refusing symbolic link ${target_env}" >&2
    exit 65
  fi
  if [[ -e "${target_env}" ]]; then
    if [[ ! -f "${target_env}" ]]; then
      echo "profile env init: existing path is not a regular file: ${target_env}" >&2
      exit 65
    fi
    chmod 0600 "${target_env}"
    existing=$((existing + 1))
    continue
  fi

  tmp_env=$(mktemp "${profile_dir}/.env.new.XXXXXX")
  cp "${source_env}" "${tmp_env}"
  chmod 0600 "${tmp_env}"
  if ! ln "${tmp_env}" "${target_env}" 2>/dev/null; then
    rm -f -- "${tmp_env}"
    echo "profile env init: ${target_env} appeared concurrently; nothing was overwritten" >&2
    exit 65
  fi
  rm -f -- "${tmp_env}"
  created=$((created + 1))
done

echo "profile env init: created ${created}, preserved ${existing} under ${profiles_root}"
echo "profile env init: fill every .env, keep mode 0600, then run ./scripts/verify-bundle.sh"
