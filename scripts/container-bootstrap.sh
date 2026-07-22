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

require_env() {
  local key=$1
  if [[ -z "${!key:-}" ]]; then
    echo "fleet bootstrap: required variable ${key} is empty" >&2
    return 1
  fi
}

validate_environment() {
  local profile key failed=0

  for key in FLEET_MODEL GITLAB_HOST FEISHU_APP_ID FEISHU_APP_SECRET \
    FEISHU_ALLOWED_USERS FEISHU_HOME_CHANNEL; do
    require_env "${key}" || failed=1
  done

  for profile in "${profiles[@]}"; do
    key="GITLAB_TOKEN_${profile^^}"
    key=${key//-/_}
    require_env "${key}" || failed=1
  done

  for profile in prd-writer fde spec-writer planner tasker coder; do
    key=${profile^^}
    key=${key//-/_}
    require_env "GIT_COMMIT_NAME_${key}" || failed=1
    require_env "GIT_COMMIT_EMAIL_${key}" || failed=1
  done

  if [[ "${GITLAB_HOST:-}" == *example* ]]; then
    echo "fleet bootstrap: GITLAB_HOST still contains the example placeholder" >&2
    failed=1
  fi
  if [[ "${FEISHU_ALLOWED_USERS:-}" == *replace_me* || \
        "${FEISHU_HOME_CHANNEL:-}" == *replace_me* ]]; then
    echo "fleet bootstrap: Feishu allowlist/channel still contains a placeholder" >&2
    failed=1
  fi

  if [[ ${failed} -ne 0 ]]; then
    echo "fleet bootstrap: fix .env and restart the container" >&2
    exit 64
  fi
}

install_profile() {
  local profile=$1
  local source_dir="${fleet_root}/profiles/${profile}"
  local target_dir="${profiles_root}/${profile}"

  install -d -o hermes -g hermes "${target_dir}" "${target_dir}/skills" \
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

write_profile_env() {
  local profile=$1
  local target_dir="${profiles_root}/${profile}"
  local token_key token_value tmp_env

  token_key="GITLAB_TOKEN_${profile^^}"
  token_key=${token_key//-/_}
  token_value=${!token_key:-}
  tmp_env=$(mktemp "${target_dir}/.env.tmp.XXXXXX")
  chmod 0600 "${tmp_env}"

  {
    printf 'GITLAB_HOST=%s\n' "${GITLAB_HOST:-}"
    printf 'GITLAB_TOKEN=%s\n' "${token_value}"
    for key in OPENAI_API_KEY OPENROUTER_API_KEY ANTHROPIC_API_KEY NOUS_API_KEY; do
      if [[ -n "${!key:-}" ]]; then
        printf '%s=%s\n' "${key}" "${!key}"
      fi
    done
    if [[ "${profile}" == dispatcher ]]; then
      for key in FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_DOMAIN \
        FEISHU_CONNECTION_MODE FEISHU_ALLOWED_USERS FEISHU_HOME_CHANNEL \
        FEISHU_GROUP_POLICY FEISHU_REQUIRE_MENTION; do
        printf '%s=%s\n' "${key}" "${!key:-}"
      done
      printf 'LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1\n'
      printf 'LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1\n'
    fi
  } > "${tmp_env}"

  chown hermes:hermes "${tmp_env}"
  mv "${tmp_env}" "${target_dir}/.env"
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
  local dispatcher_home="${profiles_root}/dispatcher/home"
  if [[ -z "${FEISHU_APP_ID:-}" || -z "${FEISHU_APP_SECRET:-}" ]]; then
    return
  fi
  if ! /command/s6-setuidgid hermes env HOME="${dispatcher_home}" lark-cli config show >/dev/null 2>&1; then
    printf '%s\n' "${FEISHU_APP_SECRET}" | \
      /command/s6-setuidgid hermes env HOME="${dispatcher_home}" \
      lark-cli config init --app-id "${FEISHU_APP_ID}" --app-secret-stdin \
      --brand "${FEISHU_DOMAIN:-feishu}" >/dev/null
  fi
}

scrub_container_environment() {
  local profile key env_file
  local keys=(
    GITLAB_HOST GITLAB_TOKEN FLEET_MODEL FLEET_FORCE_CONFIG FLEET_SYNC_SECRETS
    OPENAI_API_KEY OPENROUTER_API_KEY ANTHROPIC_API_KEY NOUS_API_KEY
    FEISHU_APP_ID FEISHU_APP_SECRET FEISHU_DOMAIN FEISHU_CONNECTION_MODE
    FEISHU_ALLOWED_USERS FEISHU_HOME_CHANNEL FEISHU_GROUP_POLICY
    FEISHU_REQUIRE_MENTION
  )

  for profile in "${profiles[@]}"; do
    key=${profile^^}
    key=${key//-/_}
    keys+=("GITLAB_TOKEN_${key}" "GIT_COMMIT_NAME_${key}" "GIT_COMMIT_EMAIL_${key}")
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
install -d -o hermes -g hermes "${profiles_root}"
for profile in "${profiles[@]}"; do
  install_profile "${profile}"
  if [[ "${FLEET_SYNC_SECRETS:-1}" == "1" || ! -f "${profiles_root}/${profile}/.env" ]]; then
    write_profile_env "${profile}"
  fi
  configure_git "${profile}"
  configure_model "${profile}"
done

configure_lark_cli
scrub_container_environment
