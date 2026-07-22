#!/usr/bin/env bash
set -euo pipefail
umask 077

fleet_root=/opt/fleet
data_root=/opt/data
profiles_root="${data_root}/profiles"

profiles=(
  dispatcher prd-writer fde spec-writer spec-reviewer planner
  plan-reviewer tasker task-reviewer coder tester code-reviewer
)
gateway_profiles=(dispatcher prd-writer fde)

require_env() {
  local key=$1
  if [[ -z "${!key:-}" ]]; then
    echo "fleet bootstrap: required variable ${key} is empty" >&2
    return 1
  fi
}

validate_environment() {
  local profile key failed=0

  for key in FLEET_MODEL; do
    require_env "${key}" || failed=1
  done

  for profile in prd-writer spec-writer planner tasker coder; do
    key=${profile^^}
    key=${key//-/_}
    require_env "GIT_COMMIT_NAME_${key}" || failed=1
    require_env "GIT_COMMIT_EMAIL_${key}" || failed=1
  done

  if [[ ${failed} -ne 0 ]]; then
    echo "fleet bootstrap: fix the deployment .env and restart the container" >&2
    exit 64
  fi
}

install_profile() {
  local profile=$1
  local source_dir="${fleet_root}/profiles/${profile}"
  local target_dir="${profiles_root}/${profile}"

  install -d -m 0700 -o hermes -g hermes "${target_dir}" "${target_dir}/skills" \
    "${target_dir}/memories" "${target_dir}/home"
  install -o hermes -g hermes -m 0640 "${source_dir}/SOUL.md" "${target_dir}/SOUL.md"
  install -o hermes -g hermes -m 0640 "${source_dir}/distribution.yaml" "${target_dir}/distribution.yaml"
  install -o hermes -g hermes -m 0640 "${source_dir}/profile.yaml" "${target_dir}/profile.yaml"

  if [[ ! -f "${target_dir}/config.yaml" || "${FLEET_FORCE_CONFIG:-0}" == "1" ]]; then
    install -o hermes -g hermes -m 0640 "${source_dir}/config.yaml" "${target_dir}/config.yaml"
  fi

  if [[ -d "${target_dir}/skills" ]]; then
    find "${target_dir}/skills" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
  fi
  cp -a "${source_dir}/skills/." "${target_dir}/skills/"
  chown -R hermes:hermes "${target_dir}/skills"

  for context_file in MEMORY.md USER.md; do
    if [[ ! -f "${target_dir}/memories/${context_file}" ]]; then
      install -o hermes -g hermes -m 0640 \
        "${source_dir}/bootstrap/memories/${context_file}" \
        "${target_dir}/memories/${context_file}"
    fi
  done
}

configure_model() {
  local profile=$1
  if [[ -n "${FLEET_MODEL:-}" ]]; then
    /command/s6-setuidgid hermes hermes -p "${profile}" \
      config set model "${FLEET_MODEL}" >/dev/null
  fi
}

configure_git() {
  local profile=$1
  local profile_home="${profiles_root}/${profile}/home"
  local key name_key email_key commit_name commit_email

  key=${profile^^}
  key=${key//-/_}
  name_key="GIT_COMMIT_NAME_${key}"
  email_key="GIT_COMMIT_EMAIL_${key}"
  commit_name=${!name_key:-}
  commit_email=${!email_key:-}

  /command/s6-setuidgid hermes env HOME="${profile_home}" \
    git config --global credential.helper '!glab auth git-credential'
  if [[ -n "${commit_name}" ]]; then
    /command/s6-setuidgid hermes env HOME="${profile_home}" \
      git config --global user.name "${commit_name}"
  fi
  if [[ -n "${commit_email}" ]]; then
    /command/s6-setuidgid hermes env HOME="${profile_home}" \
      git config --global user.email "${commit_email}"
  fi
}

configure_lark_cli() {
  local profile profile_dir profile_home lark_root config_dir data_dir
  for profile in "${gateway_profiles[@]}"; do
    profile_dir="${profiles_root}/${profile}"
    profile_home="${profile_dir}/home"
    lark_root="${profile_dir}/.lark-cli"
    config_dir="${lark_root}/config"
    data_dir="${lark_root}/data"
    install -d -m 0700 -o hermes -g hermes \
      "${lark_root}" "${config_dir}" "${data_dir}"
    /command/s6-setuidgid hermes env \
      HOME="${profile_home}" \
      HERMES_HOME="${profile_dir}" \
      LARKSUITE_CLI_CONFIG_DIR="${config_dir}" \
      LARKSUITE_CLI_DATA_DIR="${data_dir}" \
      LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1 \
      LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1 \
      lark-cli config bind --source hermes --identity bot-only >/dev/null
    find "${lark_root}" -type d -exec chmod 0700 {} +
    find "${lark_root}" -type f -exec chmod 0600 {} +
  done
}

scrub_container_environment() {
  local profile key env_file
  local keys=(
    HERMES_PROFILE API_SERVER_PORT GITLAB_HOST GITLAB_ALLOWED_GROUPS GITLAB_TOKEN
    FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_DOMAIN FEISHU_CONNECTION_MODE
    FEISHU_ALLOWED_USERS FEISHU_HOME_CHANNEL FEISHU_GROUP_POLICY
    FEISHU_REQUIRE_MENTION FLEET_FORCE_CONFIG
  )

  for profile in "${profiles[@]}"; do
    key=${profile^^}
    key=${key//-/_}
    keys+=("GIT_COMMIT_NAME_${key}" "GIT_COMMIT_EMAIL_${key}")
  done

  # s6 repopulates long-running process environments from this envdir after
  # cont-init. Empty files mean unset, so fleet-wide bootstrap credentials do
  # not leak into the dispatcher or Kanban workers.
  for key in "${keys[@]}"; do
    env_file="/run/s6/container_environment/${key}"
    if [[ -e "${env_file}" ]]; then
      : > "${env_file}"
    fi
  done
}

validate_environment
if [[ ! -d "${profiles_root}" ]]; then
  echo "fleet bootstrap: ${profiles_root} is missing; run ./scripts/init-profile-envs.sh on the host" >&2
  exit 65
fi
chmod 0700 "${profiles_root}"
for profile in "${profiles[@]}"; do
  install_profile "${profile}"
done

python3 "${fleet_root}/scripts/validate-profile-envs.py" \
  --profiles-root "${profiles_root}" --owner hermes

for profile in "${profiles[@]}"; do
  configure_git "${profile}"
  configure_model "${profile}"
done

configure_lark_cli
scrub_container_environment
