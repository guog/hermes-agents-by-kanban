#!/command/with-contenv bash
set -euo pipefail
umask 077

fleet_root=/opt/fleet
data_root=/opt/data
profiles_root="${data_root}/profiles"
skill_marker_root="${data_root}/.fleet/skills-v1"
projects_root=/workspace/projects

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

  for key in FLEET_MODEL PUID PGID; do
    require_env "${key}" || failed=1
  done
  if [[ -n "${FLEET_MODEL:-}" ]]; then
    if [[ "${FLEET_MODEL}" != */* || -z "${FLEET_MODEL%%/*}" || -z "${FLEET_MODEL#*/}" ]]; then
      echo "fleet bootstrap: FLEET_MODEL must use provider/model format" >&2
      failed=1
    fi
  fi

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

prepare_projects_root() {
  local expected_uid expected_gid actual_uid actual_gid probe

  [[ "${PUID}" =~ ^[0-9]+$ ]] || {
    echo "fleet bootstrap: PUID must be a non-negative decimal integer" >&2
    exit 65
  }
  [[ "${PGID}" =~ ^[0-9]+$ ]] || {
    echo "fleet bootstrap: PGID must be a non-negative decimal integer" >&2
    exit 65
  }
  expected_uid=$(id -u hermes)
  expected_gid=$(id -g hermes)
  [[ "${expected_uid}" == "${PUID}" && "${expected_gid}" == "${PGID}" ]] || {
    echo "fleet bootstrap: hermes identity does not match PUID:PGID ${PUID}:${PGID}" >&2
    exit 65
  }
  [[ -d "${projects_root}" && ! -L "${projects_root}" ]] || {
    echo "fleet bootstrap: ${projects_root} must be a real mounted directory" >&2
    exit 65
  }

  actual_uid=$(stat -c %u "${projects_root}")
  actual_gid=$(stat -c %g "${projects_root}")
  if [[ "${actual_uid}:${actual_gid}" != "${PUID}:${PGID}" ]]; then
    chown -- "${PUID}:${PGID}" "${projects_root}"
  fi
  chmod 0700 "${projects_root}"

  actual_uid=$(stat -c %u "${projects_root}")
  actual_gid=$(stat -c %g "${projects_root}")
  [[ "${actual_uid}:${actual_gid}" == "${PUID}:${PGID}" ]] || {
    echo "fleet bootstrap: could not assign ${projects_root} to hermes ${PUID}:${PGID}" >&2
    exit 65
  }
  probe=$(
    /command/s6-setuidgid hermes \
      mktemp -d "${projects_root}/.fleet-write-check.XXXXXX"
  ) || {
    echo "fleet bootstrap: ${projects_root} is not writable by hermes" >&2
    exit 65
  }
  /command/s6-setuidgid hermes rmdir -- "${probe}" || {
    echo "fleet bootstrap: could not remove projects write probe ${probe}" >&2
    exit 65
  }
}

install_profile() {
  local profile=$1
  local source_dir="${fleet_root}/profiles/${profile}"
  local target_dir="${profiles_root}/${profile}"
  local new_profile=0
  local -a skill_init_args

  if [[ -L "${target_dir}/config.yaml" ]]; then
    echo "fleet bootstrap: existing config must not be a symbolic link: ${target_dir}/config.yaml" >&2
    exit 65
  elif [[ ! -e "${target_dir}/config.yaml" ]]; then
    new_profile=1
  elif [[ ! -f "${target_dir}/config.yaml" ]]; then
    echo "fleet bootstrap: existing config is not a regular file: ${target_dir}/config.yaml" >&2
    exit 65
  fi

  install -d -m 0700 -o hermes -g hermes "${target_dir}" "${target_dir}/skills" \
    "${target_dir}/memories" "${target_dir}/home"
  skill_init_args=(
    --profile "${profile}"
    --source "${source_dir}/skills"
    --target "${target_dir}/skills"
    --marker-root "${skill_marker_root}"
    --owner hermes
    --group hermes
  )
  if [[ ${new_profile} -eq 1 ]]; then
    skill_init_args+=(--new-profile)
  fi
  python3 "${fleet_root}/scripts/initialize-profile-skills.py" "${skill_init_args[@]}"

  install -o hermes -g hermes -m 0640 "${source_dir}/SOUL.md" "${target_dir}/SOUL.md"
  install -o hermes -g hermes -m 0640 "${source_dir}/distribution.yaml" "${target_dir}/distribution.yaml"
  install -o hermes -g hermes -m 0640 "${source_dir}/profile.yaml" "${target_dir}/profile.yaml"

  if [[ ! -f "${target_dir}/config.yaml" || "${FLEET_FORCE_CONFIG:-0}" == "1" ]]; then
    install -o hermes -g hermes -m 0640 "${source_dir}/config.yaml" "${target_dir}/config.yaml"
  fi

  for context_file in MEMORY.md USER.md; do
    if [[ ! -f "${target_dir}/memories/${context_file}" ]]; then
      install -o hermes -g hermes -m 0640 \
        "${source_dir}/bootstrap/memories/${context_file}" \
        "${target_dir}/memories/${context_file}"
    fi
  done
}

configure_model() {
  local profile=$1 provider model
  if [[ -n "${FLEET_MODEL:-}" ]]; then
    provider=${FLEET_MODEL%%/*}
    model=${FLEET_MODEL#*/}
    /command/s6-setuidgid hermes hermes -p "${profile}" \
      config set model.provider "${provider}" >/dev/null
    /command/s6-setuidgid hermes hermes -p "${profile}" \
      config set model.default "${model}" >/dev/null
    if [[ -n "${FLEET_MODEL_BASE_URL:-}" ]]; then
      /command/s6-setuidgid hermes hermes -p "${profile}" \
        config set model.base_url "${FLEET_MODEL_BASE_URL}" >/dev/null
    fi
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
    FEISHU_ALLOW_ALL_USERS FEISHU_ALLOWED_USERS FEISHU_HOME_CHANNEL
    FEISHU_GROUP_POLICY FEISHU_REQUIRE_MENTION FEISHU_ALLOW_BOTS
    FLEET_FORCE_CONFIG
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
prepare_projects_root
if [[ ! -d "${profiles_root}" ]]; then
  echo "fleet bootstrap: ${profiles_root} is missing; run ./scripts/init-profile-envs.sh on the host" >&2
  exit 65
fi
chmod 0700 "${profiles_root}"
install -d -m 0700 -o root -g root "${skill_marker_root}"
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
