#!/usr/bin/env bash
#
# 从本机工作树向远端 Linux/AMD64 主机部署一套全新的 Hermes v4。
#
# 安全边界：
#   - 只复制代码、静态 Profile 配置、.env 凭据和固定外部资产；
#   - 永不复制本机 data/auth.json；替换时仅在远端内部保留目标实例的模型认证；
#   - 不复制 Kanban/Controller DB、session、log、cache、pending、worktree；
#   - 默认拒绝覆盖已有目录、容器、Compose 项目、端口或 Docker 子网；
#   - --replace-existing 仅清除这个精确项目的容器、卷、网络和目录，不留运行态备份；
#   - 首次启动强制使用 HOLLYSYS_CONTROLLER_MODE=preflight；
#   - 不自动执行 deep preflight、切换 active 或真实 PRD-to-merge E2E。
#
# 目标环境必须由调用方显式提供。完整参数：
#   ./scripts/deploy-remote.sh --help

set -Eeuo pipefail
IFS=$'\n\t'
umask 077

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly DEFAULT_SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

SOURCE_DIR="${HERMES_DEPLOY_SOURCE_DIR:-${DEFAULT_SOURCE_DIR}}"
REMOTE_HOST="${HERMES_DEPLOY_REMOTE_HOST:-}"
REMOTE_DIR="${HERMES_DEPLOY_REMOTE_DIR:-}"
SSH_PORT="${HERMES_DEPLOY_SSH_PORT:-22}"
COMPOSE_PROJECT="${HERMES_DEPLOY_PROJECT:-}"
HERMES_CONTAINER="${HERMES_DEPLOY_CONTAINER:-}"
CONTROLLER_CONTAINER="${HERMES_DEPLOY_CONTROLLER_CONTAINER:-}"
DASHBOARD_PORT="${HERMES_DEPLOY_DASHBOARD_PORT:-}"
DOCKER_SUBNET="${HERMES_DEPLOY_DOCKER_SUBNET:-}"
IMAGE_REF="${HERMES_DEPLOY_IMAGE:-}"
IMAGE_REF_EXPLICIT=0
[[ -z "${IMAGE_REF}" ]] || IMAGE_REF_EXPLICIT=1
BUILD_NETWORK="${HERMES_DEPLOY_BUILD_NETWORK:-host}"
HEALTH_TIMEOUT="${HERMES_DEPLOY_HEALTH_TIMEOUT:-600}"

REPLACE_EXISTING=0
ASSUME_YES=0
DRY_RUN=0
SKIP_BUILD=0
REFRESH_ASSETS=0
ALLOW_MISSING_MODEL_AUTH=0

LOCAL_TMP_DIR=""
LOCAL_STAGE_DIR=""
REMOTE_INCOMING=""
SSH_CONTROL_PATH=""
SSH_CONNECTION_OPEN=0
DEPLOYMENT_ID="$(date -u '+%Y%m%dT%H%M%SZ')"

readonly EXPECTED_PROFILES=(
  code-reviewer
  coder
  dispatcher
  fde
  plan-reviewer
  planner
  prd-writer
  spec-reviewer
  spec-writer
  task-reviewer
  tasker
  tester
)
readonly GATEWAY_PROFILES=(dispatcher fde prd-writer)

usage() {
  cat <<'EOF'
用法：
  scripts/deploy-remote.sh [选项]

必填目标参数：
  --host
  --remote-dir
  --project
  --dashboard-port
  --subnet

也可通过同名环境变量提供：
  HERMES_DEPLOY_REMOTE_HOST
  HERMES_DEPLOY_REMOTE_DIR
  HERMES_DEPLOY_PROJECT
  HERMES_DEPLOY_DASHBOARD_PORT
  HERMES_DEPLOY_DOCKER_SUBNET

选项：
  --host USER@HOST          SSH 目标（必填）
  --ssh-port PORT           SSH 端口，默认 22
  --source-dir DIR          本地仓库/工作树，默认脚本所在仓库
  --remote-dir DIR          远端绝对部署目录（必填）
  --project NAME            独立 Compose 项目名（必填）
  --hermes-container NAME   Hermes 容器名，默认 <project>-hermes
  --controller-container N  Controller 容器名，默认 <project>-controller
  --dashboard-port PORT     Dashboard 宿主机端口（必填）
  --subnet CIDR             独立 Docker IPv4 子网（必填）
  --image IMAGE:TAG         派生镜像引用；默认生成带 UTC 时间的唯一 tag
  --build-network MODE      docker build 网络：host（默认）、default 或 none
  --health-timeout SEC      等待两个容器 healthy 的秒数，默认 600
  --refresh-assets          打包前运行 npm ci 和 npm run assets:install
  --skip-build              不构建镜像，要求远端已存在 --image 指定的镜像
  --allow-missing-auth      兼容旧调用；本机 data/auth.json 始终不会上传
  --replace-existing       彻底清除并替换同目录、同项目的已有实例
  --yes                     与 --replace-existing 一起使用，取消交互确认
  --dry-run                 执行本地打包和远端只读预检，不上传、不构建、不启动
  -h, --help                显示帮助

示例：
  scripts/deploy-remote.sh \
    --host deploy@example.internal \
    --remote-dir /srv/hermes/fleet-a \
    --project fleet-a \
    --dashboard-port 9119 \
    --subnet 10.253.252.0/29

  scripts/deploy-remote.sh \
    --host deploy@example.internal \
    --remote-dir /srv/hermes/fleet-a \
    --project fleet-a \
    --dashboard-port 9119 \
    --subnet 10.253.252.0/29 \
    --replace-existing --yes

说明：
  1. 本地 .env、12 份 Profile .env 和 Dispatcher token 镜像会以 0600 传输。
  2. 本机 data/auth.json 始终排除；替换部署只在远端内部保留目标实例的认证文件。
  3. Profile 的实际 lark-cli config.json 不会复制；Controller 依据三份 .env 重新生成。
  4. 部署成功只证明静态 preflight、容器健康和本机 Dashboard 可达；不会自动执行
     deep preflight、真实飞书验收或 PRD→MR→checked-head merge E2E。
EOF
}

log() {
  printf '[deploy] %s\n' "$*" >&2
}

warn() {
  printf '[deploy] WARNING: %s\n' "$*" >&2
}

die() {
  printf '[deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

require_arg() {
  local option="$1"
  local value="${2:-}"
  [[ -n "${value}" ]] || die "${option} 缺少参数"
}

while (($# > 0)); do
  case "$1" in
    --host)
      require_arg "$1" "${2:-}"
      REMOTE_HOST="$2"
      shift 2
      ;;
    --ssh-port)
      require_arg "$1" "${2:-}"
      SSH_PORT="$2"
      shift 2
      ;;
    --source-dir)
      require_arg "$1" "${2:-}"
      SOURCE_DIR="$2"
      shift 2
      ;;
    --remote-dir)
      require_arg "$1" "${2:-}"
      REMOTE_DIR="$2"
      shift 2
      ;;
    --project)
      require_arg "$1" "${2:-}"
      COMPOSE_PROJECT="$2"
      shift 2
      ;;
    --hermes-container)
      require_arg "$1" "${2:-}"
      HERMES_CONTAINER="$2"
      shift 2
      ;;
    --controller-container)
      require_arg "$1" "${2:-}"
      CONTROLLER_CONTAINER="$2"
      shift 2
      ;;
    --dashboard-port)
      require_arg "$1" "${2:-}"
      DASHBOARD_PORT="$2"
      shift 2
      ;;
    --subnet)
      require_arg "$1" "${2:-}"
      DOCKER_SUBNET="$2"
      shift 2
      ;;
    --image)
      require_arg "$1" "${2:-}"
      IMAGE_REF="$2"
      IMAGE_REF_EXPLICIT=1
      shift 2
      ;;
    --build-network)
      require_arg "$1" "${2:-}"
      BUILD_NETWORK="$2"
      shift 2
      ;;
    --health-timeout)
      require_arg "$1" "${2:-}"
      HEALTH_TIMEOUT="$2"
      shift 2
      ;;
    --refresh-assets)
      REFRESH_ASSETS=1
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --allow-missing-auth)
      ALLOW_MISSING_MODEL_AUTH=1
      shift
      ;;
    --replace-existing)
      REPLACE_EXISTING=1
      shift
      ;;
    --yes)
      ASSUME_YES=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      (($# == 0)) || die "不接受位置参数：$*"
      ;;
    -*)
      die "未知选项：$1（使用 --help 查看帮助）"
      ;;
    *)
      die "不接受位置参数：$1"
      ;;
  esac
done

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_command() {
  command_exists "$1" || die "本机缺少命令：$1"
}

validate_port() {
  local value="$1"
  local label="$2"
  [[ "${value}" =~ ^[0-9]+$ ]] || die "${label} 必须是整数：${value}"
  ((10#${value} >= 1 && 10#${value} <= 65535)) ||
    die "${label} 必须在 1..65535：${value}"
}

validate_positive_integer() {
  local value="$1"
  local label="$2"
  [[ "${value}" =~ ^[0-9]+$ ]] || die "${label} 必须是正整数：${value}"
  ((10#${value} > 0)) || die "${label} 必须大于 0：${value}"
}

validate_ipv4_cidr() {
  local value="$1"
  local ip prefix octet
  local -a octets

  [[ "${value}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]] ||
    die "Docker 子网不是合法 IPv4 CIDR：${value}"
  ip="${value%/*}"
  prefix="${value#*/}"
  IFS='.' read -r -a octets <<<"${ip}"
  for octet in "${octets[@]}"; do
    ((10#${octet} <= 255)) || die "Docker 子网包含非法 IPv4 地址：${value}"
  done
  ((10#${prefix} >= 24 && 10#${prefix} <= 30)) ||
    die "Docker 子网必须是 /24 到 /30 的小型专用网段：${value}"
}

validate_inputs() {
  local resolved_source
  local -a missing=()

  require_command awk
  require_command chmod
  require_command cp
  require_command find
  require_command git
  require_command mktemp
  require_command node
  require_command npm
  require_command rsync
  require_command ssh

  [[ -d "${SOURCE_DIR}" ]] || die "本地源目录不存在：${SOURCE_DIR}"
  resolved_source="$(cd -- "${SOURCE_DIR}" && pwd -P)"
  SOURCE_DIR="${resolved_source}"

  [[ -n "${REMOTE_HOST}" ]] || missing+=(--host)
  [[ -n "${REMOTE_DIR}" ]] || missing+=(--remote-dir)
  [[ -n "${COMPOSE_PROJECT}" ]] || missing+=(--project)
  [[ -n "${DASHBOARD_PORT}" ]] || missing+=(--dashboard-port)
  [[ -n "${DOCKER_SUBNET}" ]] || missing+=(--subnet)
  if ((${#missing[@]} > 0)); then
    die "缺少必填目标参数：${missing[*]}（使用 --help 查看示例）"
  fi

  if [[ -z "${HERMES_CONTAINER}" ]]; then
    HERMES_CONTAINER="${COMPOSE_PROJECT}-hermes"
  fi
  if [[ -z "${CONTROLLER_CONTAINER}" ]]; then
    CONTROLLER_CONTAINER="${COMPOSE_PROJECT}-controller"
  fi

  [[ "${REMOTE_HOST}" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$ ]] ||
    die "SSH 目标格式不安全，应为 USER@HOST：${REMOTE_HOST}"
  validate_port "${SSH_PORT}" "SSH 端口"
  validate_port "${DASHBOARD_PORT}" "Dashboard 端口"
  validate_positive_integer "${HEALTH_TIMEOUT}" "健康等待时间"

  [[ "${REMOTE_DIR}" =~ ^/[A-Za-z0-9._/-]+$ ]] ||
    die "远端目录必须是仅含安全字符的绝对路径：${REMOTE_DIR}"
  [[ "${REMOTE_DIR}" != "/" && "${REMOTE_DIR}" != "/home" &&
    "${REMOTE_DIR}" != "/opt" && "${REMOTE_DIR}" != "/var" &&
    "${REMOTE_DIR}" != "/usr" && "${REMOTE_DIR}" != "/etc" &&
    "${REMOTE_DIR}" != "/srv" ]] ||
    die "拒绝使用过宽的远端目录：${REMOTE_DIR}"
  [[ "/${REMOTE_DIR#/}/" != *"/../"* && "/${REMOTE_DIR#/}/" != *"/./"* &&
    "${REMOTE_DIR}" != *"//"* ]] ||
    die "远端目录不能包含 .、.. 或重复斜线：${REMOTE_DIR}"

  [[ "${COMPOSE_PROJECT}" =~ ^[a-z0-9][a-z0-9_-]{1,62}$ ]] ||
    die "Compose 项目名不安全：${COMPOSE_PROJECT}"
  [[ "${HERMES_CONTAINER}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$ ]] ||
    die "Hermes 容器名不安全：${HERMES_CONTAINER}"
  [[ "${CONTROLLER_CONTAINER}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{1,62}$ ]] ||
    die "Controller 容器名不安全：${CONTROLLER_CONTAINER}"
  [[ "${HERMES_CONTAINER}" != "${CONTROLLER_CONTAINER}" ]] ||
    die "两个容器不能同名"

  validate_ipv4_cidr "${DOCKER_SUBNET}"
  case "${BUILD_NETWORK}" in
    host | default | none) ;;
    *) die "--build-network 只接受 host、default 或 none" ;;
  esac

  if [[ -z "${IMAGE_REF}" ]]; then
    IMAGE_REF="hollysys/hermes-agent:v4-${COMPOSE_PROJECT}-${DEPLOYMENT_ID}"
  fi
  [[ "${IMAGE_REF}" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] ||
    die "镜像引用必须是安全的 IMAGE:TAG：${IMAGE_REF}"
  if ((SKIP_BUILD == 1 && IMAGE_REF_EXPLICIT == 0)); then
    die "--skip-build 必须同时显式传入 --image 或设置 HERMES_DEPLOY_IMAGE"
  fi

  if ((ASSUME_YES == 1 && REPLACE_EXISTING == 0)); then
    warn "--yes 只对 --replace-existing 生效"
  fi
}

read_dotenv_value() {
  local file="$1"
  local key="$2"
  local line value

  line="$(
    awk -v wanted="${key}" '
      index($0, wanted "=") == 1 {
        sub(/\r$/, "")
        print
        found = 1
        exit
      }
      END { if (!found) exit 1 }
    ' "${file}"
  )" || return 1
  value="${line#*=}"
  if [[ "${value}" == \'*\' && "${#value}" -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  elif [[ "${value}" == \"*\" && "${#value}" -ge 2 ]]; then
    value="${value:1:${#value}-2}"
  fi
  printf '%s' "${value}"
}

require_dotenv_value() {
  local file="$1"
  local key="$2"
  local label="$3"
  local value

  value="$(read_dotenv_value "${file}" "${key}")" ||
    die "${label} 缺少 ${key}"
  [[ -n "${value}" ]] || die "${label} 的 ${key} 为空"
}

set_dotenv_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local temporary="${file}.tmp.$$"

  awk -v wanted="${key}" -v replacement="${value}" '
    index($0, wanted "=") == 1 {
      if (!written) {
        print wanted "=" replacement
        written = 1
      }
      next
    }
    { print }
    END {
      if (!written) print wanted "=" replacement
    }
  ' "${file}" >"${temporary}"
  chmod 0600 "${temporary}"
  mv -f -- "${temporary}" "${file}"
}

validate_local_configuration() {
  local profile profile_env actual_profile gateway
  local profile_count=0

  local -a required_files=(
    .env
    .env.example
    Dockerfile
    docker-compose.yaml
    requirements-controller.txt
    external-assets.json
    package-lock.json
    package.json
    hollysysctl
    controller/config.yaml
    container/run-hollysys-controller.sh
    scripts/install-external-assets.mjs
  )

  log "校验本地配置和凭据（不输出秘密）"
  for profile in "${required_files[@]}"; do
    [[ -f "${SOURCE_DIR}/${profile}" ]] || die "缺少本地文件：${profile}"
  done

  [[ ! -L "${SOURCE_DIR}/.env" ]] || die "拒绝使用符号链接 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_GITLAB_HOST" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_GITLAB_ALLOWED_GROUPS" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_SPEC_REVIEWER_IDENTITIES" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_PLAN_REVIEWER_IDENTITIES" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_TASKS_REVIEWER_IDENTITIES" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_TESTER_IDENTITIES" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HOLLYSYS_CODE_REVIEWER_IDENTITIES" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HERMES_DASHBOARD_BASIC_AUTH_USERNAME" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" "根 .env"
  require_dotenv_value "${SOURCE_DIR}/.env" "HERMES_DASHBOARD_BASIC_AUTH_SECRET" "根 .env"

  for profile in "${EXPECTED_PROFILES[@]}"; do
    profile_env="${SOURCE_DIR}/data/profiles/${profile}/.env"
    [[ -d "${SOURCE_DIR}/data/profiles/${profile}" ]] ||
      die "缺少 Profile 目录：${profile}"
    [[ -f "${profile_env}" && ! -L "${profile_env}" ]] ||
      die "缺少普通文件 data/profiles/${profile}/.env"
    require_dotenv_value "${profile_env}" "HERMES_PROFILE" "${profile} .env"
    require_dotenv_value "${profile_env}" "GITLAB_HOST" "${profile} .env"
    require_dotenv_value "${profile_env}" "GITLAB_ALLOWED_GROUPS" "${profile} .env"
    require_dotenv_value "${profile_env}" "GITLAB_TOKEN" "${profile} .env"
    actual_profile="$(read_dotenv_value "${profile_env}" "HERMES_PROFILE")"
    [[ "${actual_profile}" == "${profile}" ]] ||
      die "${profile} .env 中 HERMES_PROFILE=${actual_profile}，与目录名不一致"
    ((profile_count += 1))
  done
  ((profile_count == 12)) || die "Profile 数量不是 12"

  for gateway in "${GATEWAY_PROFILES[@]}"; do
    profile_env="${SOURCE_DIR}/data/profiles/${gateway}/.env"
    require_dotenv_value "${profile_env}" "FEISHU_APP_ID" "${gateway} .env"
    require_dotenv_value "${profile_env}" "FEISHU_APP_SECRET" "${gateway} .env"
    require_dotenv_value "${profile_env}" "API_SERVER_PORT" "${gateway} .env"
    [[ -f "${SOURCE_DIR}/data/profiles/${gateway}/gateway_state.json" ]] ||
      die "缺少 ${gateway}/gateway_state.json"
  done

  if [[ -e "${SOURCE_DIR}/data/auth.json" ]]; then
    log "本机 data/auth.json 已明确排除，不会进入部署包"
  elif ((ALLOW_MISSING_MODEL_AUTH == 0)); then
    warn "本机没有 data/auth.json；全新目标需在远端人工完成 Codex 登录"
  fi

  if ((REFRESH_ASSETS == 1)); then
    log "按锁文件重新安装固定外部 Skills 和 Linux/AMD64 CLI"
    (
      cd -- "${SOURCE_DIR}"
      npm ci
      npm run assets:install
    )
  fi

  node --input-type=module - "${SOURCE_DIR}" <<'NODE'
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { join } from "node:path";

const root = process.argv[2];
const config = JSON.parse(await readFile(join(root, "external-assets.json"), "utf8"));
for (const tool of config.cli) {
  const path = join(root, "cli", "bin", tool.installAs);
  let bytes;
  try {
    bytes = await readFile(path);
  } catch {
    process.stderr.write(`[deploy] ERROR: 缺少固定 CLI：cli/bin/${tool.installAs}\n`);
    process.exit(1);
  }
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== tool.binarySha256) {
    process.stderr.write(`[deploy] ERROR: cli/bin/${tool.installAs} SHA-256 不匹配\n`);
    process.exit(1);
  }
}
for (const source of config.skills) {
  for (const skill of source.skills) {
    const path = join(root, "skills", source.group, skill, "SKILL.md");
    try {
      await readFile(path);
    } catch {
      process.stderr.write(`[deploy] ERROR: 缺少固定 Skill：skills/${source.group}/${skill}/SKILL.md\n`);
      process.exit(1);
    }
  }
}
NODE
}

init_ssh() {
  # macOS limits Unix-domain socket paths to 104 bytes. Keep the ControlPath
  # intentionally short instead of inheriting its long per-user TMPDIR.
  LOCAL_TMP_DIR="$(mktemp -d "/tmp/hermes-deploy.XXXXXXXX")"
  chmod 0700 "${LOCAL_TMP_DIR}"
  LOCAL_STAGE_DIR="${LOCAL_TMP_DIR}/payload"
  SSH_CONTROL_PATH="${LOCAL_TMP_DIR}/c"
}

ssh_run() {
  ssh \
    -p "${SSH_PORT}" \
    -o ConnectTimeout=15 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    -o StrictHostKeyChecking=accept-new \
    -o ControlMaster=auto \
    -o ControlPersist=300 \
    -o "ControlPath=${SSH_CONTROL_PATH}" \
    "${REMOTE_HOST}" "$@"
}

rsync_shell() {
  printf 'ssh -p %q -o ConnectTimeout=15 -o ServerAliveInterval=15 ' "${SSH_PORT}"
  printf '%s ' '-o ServerAliveCountMax=4' '-o StrictHostKeyChecking=accept-new'
  printf '%s ' '-o ControlMaster=auto' '-o ControlPersist=300'
  printf '%s ' '-o' "ControlPath=${SSH_CONTROL_PATH}"
}

remote_cleanup_incoming() {
  [[ -n "${REMOTE_INCOMING}" ]] || return 0
  ssh_run bash -s -- "${REMOTE_INCOMING}" <<'REMOTE_CLEANUP' >/dev/null 2>&1 || true
set -eu
target="$1"
case "${target}" in
  /*.incoming.[0-9]*.[0-9]*) ;;
  *) exit 1 ;;
esac
if [ -e "${target}" ]; then
  rm -rf -- "${target}"
fi
REMOTE_CLEANUP
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM HUP
  if ((status != 0 && DRY_RUN == 0)); then
    remote_cleanup_incoming
  fi
  if ((SSH_CONNECTION_OPEN == 1)); then
    ssh \
      -p "${SSH_PORT}" \
      -o "ControlPath=${SSH_CONTROL_PATH}" \
      -O exit "${REMOTE_HOST}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${LOCAL_TMP_DIR}" && -d "${LOCAL_TMP_DIR}" ]]; then
    rm -rf -- "${LOCAL_TMP_DIR}"
  fi
  exit "${status}"
}

remote_preflight() {
  local replace="$1"

  log "执行远端只读预检：${REMOTE_HOST}"
  ssh_run bash -s -- \
    "${REMOTE_DIR}" \
    "${COMPOSE_PROJECT}" \
    "${HERMES_CONTAINER}" \
    "${CONTROLLER_CONTAINER}" \
    "${DASHBOARD_PORT}" \
    "${DOCKER_SUBNET}" \
    "${replace}" <<'REMOTE_PREFLIGHT'
set -eu

remote_dir="$1"
project="$2"
hermes_container="$3"
controller_container="$4"
dashboard_port="$5"
subnet="$6"
replace="$7"

fail() {
  printf '[remote-preflight] ERROR: %s\n' "$*" >&2
  exit 1
}

[ "$(uname -s)" = "Linux" ] || fail "目标必须是 Linux"
case "$(uname -m)" in
  x86_64|amd64) ;;
  *) fail "目标必须是 Linux/AMD64，实际为 $(uname -m)" ;;
esac

command -v docker >/dev/null 2>&1 || fail "缺少 docker"
command -v rsync >/dev/null 2>&1 || fail "缺少 rsync"
command -v sha256sum >/dev/null 2>&1 || fail "缺少 sha256sum"
docker info >/dev/null 2>&1 || fail "当前 SSH 用户不能访问 Docker daemon"
docker compose version >/dev/null 2>&1 || fail "缺少 docker compose v2"

parent="$(dirname -- "${remote_dir}")"
[ -d "${parent}" ] || fail "远端父目录不存在：${parent}"
[ -w "${parent}" ] || fail "远端父目录不可写：${parent}"

available_kb="$(df -Pk "${parent}" | awk 'NR == 2 {print $4}')"
case "${available_kb}" in
  ''|*[!0-9]*) fail "无法读取远端可用空间" ;;
esac
[ "${available_kb}" -ge 5242880 ] ||
  fail "远端可用空间少于 5 GiB"

if [ -e "${remote_dir}" ]; then
  [ ! -L "${remote_dir}" ] || fail "远端目录不能是符号链接"
  if [ "${replace}" != "1" ]; then
    if [ -d "${remote_dir}" ] &&
       [ -z "$(find "${remote_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
      :
    else
      fail "远端目录已存在且非空；全新部署拒绝覆盖：${remote_dir}"
    fi
  else
    [ -d "${remote_dir}" ] || fail "替换目标不是普通目录：${remote_dir}"
    [ ! -L "${remote_dir}" ] || fail "替换目标不能是符号链接"
    [ -f "${remote_dir}/docker-compose.yaml" ] ||
      fail "替换目标缺少 docker-compose.yaml"
    [ -f "${remote_dir}/.env" ] || fail "替换目标缺少 .env"
  fi
fi

check_container() {
  name="$1"
  service="$2"
  id="$(docker ps -aq --filter "name=^/${name}$")"
  [ -z "${id}" ] && return 0
  if [ "${replace}" != "1" ]; then
    fail "容器名已存在：${name}"
  fi
  actual_project="$(
    docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' "${id}"
  )"
  actual_service="$(
    docker inspect --format '{{ index .Config.Labels "com.docker.compose.service" }}' "${id}"
  )"
  [ "${actual_project}" = "${project}" ] ||
    fail "容器 ${name} 不属于目标 Compose 项目 ${project}"
  [ "${actual_service}" = "${service}" ] ||
    fail "容器 ${name} 的 service 不是 ${service}"
}

check_container "${hermes_container}" hermes
check_container "${controller_container}" controller

project_network="${project}_default"
project_ids="$(
  docker ps -aq --filter "label=com.docker.compose.project=${project}"
)"
for project_id in ${project_ids}; do
  project_name="$(docker inspect --format '{{.Name}}' "${project_id}")"
  project_name="${project_name#/}"
  case "${project_name}" in
    "${hermes_container}"|"${controller_container}") ;;
    *) fail "Compose 项目 ${project} 还包含非目标容器：${project_name}" ;;
  esac
done

if docker network inspect "${project_network}" >/dev/null 2>&1 &&
   [ "${replace}" != "1" ]; then
  fail "Compose 网络已存在：${project_network}"
fi

network_rows="$(
  for network_id in $(docker network ls -q); do
    docker network inspect --format \
      '{{.Name}}{{range .IPAM.Config}} {{.Subnet}}{{end}}' "${network_id}"
  done
)"
while IFS= read -r row; do
  [ -n "${row}" ] || continue
  network_name="${row%% *}"
  case " ${row#* } " in
    *" ${subnet} "*)
      if [ "${network_name}" != "${project_network}" ]; then
        fail "Docker 子网 ${subnet} 已被网络 ${network_name} 使用"
      fi
      ;;
  esac
done <<EOF_NETWORKS
${network_rows}
EOF_NETWORKS

if command -v ss >/dev/null 2>&1 &&
   ss -H -ltn 2>/dev/null |
     awk '{print $4}' |
     grep -Eq "(^|[:.])${dashboard_port}$"; then
  if [ "${replace}" != "1" ]; then
    fail "Dashboard 端口已被占用：${dashboard_port}"
  fi
  existing_hermes_id="$(
    docker ps -q --filter "name=^/${hermes_container}$"
  )"
  [ -n "${existing_hermes_id}" ] ||
    fail "Dashboard 端口被非目标进程占用：${dashboard_port}"
  published_port="$(
    docker inspect --format \
      '{{with (index (index .HostConfig.PortBindings "9119/tcp") 0)}}{{.HostPort}}{{end}}' \
      "${existing_hermes_id}"
  )"
  [ "${published_port}" = "${dashboard_port}" ] ||
    fail "Dashboard 端口被非目标进程占用：${dashboard_port}"
fi

if [ "${replace}" != "1" ]; then
  if command -v ip >/dev/null 2>&1 &&
     ip -4 route show | awk '{print $1}' | grep -Fxq "${subnet}"; then
    fail "目标机路由表已存在精确网段：${subnet}"
  fi
fi

printf '[remote-preflight] host=%s arch=%s uid=%s gid=%s free_gib=%s\n' \
  "$(hostname)" "$(uname -m)" "$(id -u)" "$(id -g)" \
  "$((available_kb / 1024 / 1024))"
printf '[remote-preflight] existing Hermes containers (read-only):\n'
docker ps -a --filter name=hermes \
  --format '  {{.Names}}\t{{.Status}}\t{{.Ports}}' || true
printf 'REMOTE_UID=%s\n' "$(id -u)"
printf 'REMOTE_GID=%s\n' "$(id -g)"
REMOTE_PREFLIGHT
  SSH_CONNECTION_OPEN=1
}

prepare_local_stage() {
  local remote_uid="$1"
  local remote_gid="$2"
  local profile dispatcher_token source_commit source_branch source_dirty

  log "构造无运行态的全新部署包"
  mkdir -p "${LOCAL_STAGE_DIR}"

  rsync -a \
    --exclude='/.git/' \
    --exclude='/.env' \
    --exclude='/.venv/' \
    --exclude='/data/' \
    --exclude='/projects/' \
    --exclude='/controller-data/' \
    --exclude='/controller-socket/' \
    --exclude='/secrets/' \
    --exclude='/node_modules/' \
    --exclude='/reports/' \
    --exclude='/tmp/' \
    --exclude='/.external-assets-stage-*/' \
    --exclude='/.pytest_cache/' \
    --exclude='/.ruff_cache/' \
    --exclude='/.mypy_cache/' \
    --exclude='/.coverage' \
    --exclude='/htmlcov/' \
    --exclude='__pycache__/' \
    --exclude='*.py[co]' \
    --exclude='*.log' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    "${SOURCE_DIR}/" "${LOCAL_STAGE_DIR}/"

  mkdir -p "${LOCAL_STAGE_DIR}/data/profiles"
  rsync -a \
    --exclude='auth.json' \
    --exclude='.lark-cli/config/hermes/config.json' \
    --exclude='.lark-cli/data/' \
    --exclude='home/.config/glab-cli/' \
    --exclude='home/.ssh/' \
    --exclude='backups/' \
    --exclude='checkpoints/' \
    --exclude='cron/' \
    --exclude='hooks/' \
    --exclude='logs/' \
    --exclude='pending/' \
    --exclude='sessions/' \
    --exclude='state-snapshots/' \
    --exclude='*.db' \
    --exclude='*.db-*' \
    --exclude='*.jsonl' \
    --exclude='__pycache__/' \
    --exclude='*.py[co]' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    "${SOURCE_DIR}/data/profiles/" "${LOCAL_STAGE_DIR}/data/profiles/"

  cp -p -- "${SOURCE_DIR}/.env" "${LOCAL_STAGE_DIR}/.env"
  chmod 0600 "${LOCAL_STAGE_DIR}/.env"

  [[ ! -e "${LOCAL_STAGE_DIR}/data/auth.json" ]] ||
    die "部署包不得包含本机 data/auth.json"

  mkdir -p \
    "${LOCAL_STAGE_DIR}/data/scratch" \
    "${LOCAL_STAGE_DIR}/data/cache/npm" \
    "${LOCAL_STAGE_DIR}/data/cache/nuget" \
    "${LOCAL_STAGE_DIR}/projects" \
    "${LOCAL_STAGE_DIR}/controller-data" \
    "${LOCAL_STAGE_DIR}/secrets"
  chmod 0700 \
    "${LOCAL_STAGE_DIR}/data" \
    "${LOCAL_STAGE_DIR}/projects" \
    "${LOCAL_STAGE_DIR}/controller-data" \
    "${LOCAL_STAGE_DIR}/secrets"

  dispatcher_token="$(
    read_dotenv_value \
      "${SOURCE_DIR}/data/profiles/dispatcher/.env" \
      GITLAB_TOKEN
  )"
  [[ -n "${dispatcher_token}" ]] || die "Dispatcher GITLAB_TOKEN 为空"
  printf '%s' "${dispatcher_token}" \
    >"${LOCAL_STAGE_DIR}/secrets/controller-gitlab-token"
  chmod 0600 "${LOCAL_STAGE_DIR}/secrets/controller-gitlab-token"
  unset dispatcher_token

  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" COMPOSE_PROJECT_NAME "${COMPOSE_PROJECT}"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" HERMES_CONTAINER_NAME "${HERMES_CONTAINER}"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HOLLYSYS_CONTROLLER_CONTAINER_NAME \
    "${CONTROLLER_CONTAINER}"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" HOLLYSYS_IMAGE "${IMAGE_REF}"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" HERMES_DATA_DIR "./data"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" PROJECTS_DIR "./projects"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HOLLYSYS_CONTROLLER_DATA_DIR \
    "./controller-data"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" PUID "${remote_uid}"
  set_dotenv_value "${LOCAL_STAGE_DIR}/.env" PGID "${remote_gid}"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HOLLYSYS_DOCKER_SUBNET \
    "${DOCKER_SUBNET}"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HERMES_DASHBOARD_HOST_PORT \
    "${DASHBOARD_PORT}"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HOLLYSYS_CONTROLLER_GITLAB_TOKEN_FILE \
    "./secrets/controller-gitlab-token"
  set_dotenv_value \
    "${LOCAL_STAGE_DIR}/.env" \
    HOLLYSYS_CONTROLLER_MODE \
    "preflight"

  source_commit="$(git -C "${SOURCE_DIR}" rev-parse HEAD)"
  source_branch="$(git -C "${SOURCE_DIR}" branch --show-current)"
  [[ -n "${source_branch}" ]] || source_branch="DETACHED"
  if [[ -n "$(git -C "${SOURCE_DIR}" status --porcelain=v1 --untracked-files=all)" ]]; then
    source_dirty=1
  else
    source_dirty=0
  fi
  {
    printf 'source_schema=1\n'
    printf 'source_commit=%s\n' "${source_commit}"
    printf 'source_branch=%s\n' "${source_branch}"
    printf 'source_dirty=%s\n' "${source_dirty}"
    printf 'packaged_at=%s\n' "${DEPLOYMENT_ID}"
  } >"${LOCAL_STAGE_DIR}/.hollysys-source"
  chmod 0600 "${LOCAL_STAGE_DIR}/.hollysys-source"

  for profile in "${EXPECTED_PROFILES[@]}"; do
    chmod 0600 "${LOCAL_STAGE_DIR}/data/profiles/${profile}/.env"
  done

  find "${LOCAL_STAGE_DIR}" -type l -print -quit |
    grep -q . &&
    die "部署包中存在符号链接，拒绝上传"

  local -a forbidden=(
    data/kanban.db
    data/kanban.db-shm
    data/kanban.db-wal
    data/sessions
    data/logs
    data/pending
    data/workspace
    controller-data/controller.db
  )
  for profile in "${forbidden[@]}"; do
    [[ ! -e "${LOCAL_STAGE_DIR}/${profile}" ]] ||
      die "部署包意外包含运行态路径：${profile}"
  done

  if find "${LOCAL_STAGE_DIR}/data/profiles" \
    \( -name 'auth.json' -o -name '*.db' -o -name '*.db-*' -o -name '*.jsonl' \) \
    -print -quit |
    grep -q .; then
    die "部署包意外包含 Profile 运行态或覆盖级认证文件"
  fi

  if find "${LOCAL_STAGE_DIR}/data/profiles" \
    -path '*/.lark-cli/config/hermes/config.json' -print -quit |
    grep -q .; then
    die "部署包意外包含旧 lark-cli config.json"
  fi

  node --input-type=module - \
    "${LOCAL_STAGE_DIR}/data/profiles" \
    "${#EXPECTED_PROFILES[@]}" <<'NODE'
import { readdir } from "node:fs/promises";

const root = process.argv[2];
const expected = Number(process.argv[3]);
const entries = (await readdir(root, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort();
if (entries.length !== expected) {
  process.stderr.write(
    `[deploy] ERROR: 打包后的 Profile 目录数为 ${entries.length}，期望 ${expected}\n`,
  );
  process.exit(1);
}
NODE

  node --input-type=module - "${LOCAL_STAGE_DIR}" \
    >"${LOCAL_STAGE_DIR}/.hollysys-manifest.sha256" <<'NODE'
import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, readdir } from "node:fs/promises";
import { join, relative, sep } from "node:path";

const root = process.argv[2];
const excluded = new Set([
  ".env",
  ".hollysys-manifest.sha256",
  "data/auth.json",
  "secrets/controller-gitlab-token",
]);
const files = [];

async function visit(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    const name = relative(root, path).split(sep).join("/");
    if (entry.isDirectory()) {
      await visit(path);
    } else if (entry.isFile()) {
      if (excluded.has(name) || /^data\/profiles\/[^/]+\/\.env$/.test(name)) continue;
      files.push({ name, path });
    } else {
      throw new Error(`部署包包含非普通文件：${name}`);
    }
  }
}

async function sha256(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

await visit(root);
files.sort((left, right) => left.name.localeCompare(right.name));
for (const file of files) {
  const info = await lstat(file.path);
  if (!info.isFile()) throw new Error(`部署包包含非普通文件：${file.name}`);
  process.stdout.write(`${await sha256(file.path)}  ${file.name}\n`);
}
NODE
  chmod 0600 "${LOCAL_STAGE_DIR}/.hollysys-manifest.sha256"

  if ((source_dirty == 1)); then
    warn "本地工作树含未提交修改；远端将记录 commit、branch、dirty=1 和 payload manifest"
  fi
  log "部署包已生成：12 Profiles，Controller=preflight，运行态=0"
}

confirm_replacement() {
  ((REPLACE_EXISTING == 1)) || return 0
  ((ASSUME_YES == 0)) || return 0

  if [[ ! -t 0 ]]; then
    die "非交互环境替换已有部署时必须显式使用 --yes"
  fi
  printf '将停止并备份远端同名部署 %s，输入项目名 %s 继续：' \
    "${REMOTE_DIR}" "${COMPOSE_PROJECT}" >&2
  local answer
  IFS= read -r answer
  [[ "${answer}" == "${COMPOSE_PROJECT}" ]] || die "已取消替换"
}

upload_stage() {
  local rsync_rsh
  REMOTE_INCOMING="${REMOTE_DIR}.incoming.${DEPLOYMENT_ID}.$$"
  rsync_rsh="$(rsync_shell)"

  log "上传到远端临时目录：${REMOTE_INCOMING}"
  ssh_run bash -s -- "${REMOTE_INCOMING}" <<'REMOTE_MKDIR'
set -eu
target="$1"
case "${target}" in
  /*.incoming.[0-9]*.[0-9]*) ;;
  *) printf '不安全的临时目录：%s\n' "${target}" >&2; exit 1 ;;
esac
[ ! -e "${target}" ] || {
  printf '临时目录已存在：%s\n' "${target}" >&2
  exit 1
}
mkdir -m 0700 -- "${target}"
REMOTE_MKDIR

  rsync -a --delete --human-readable --stats \
    -e "${rsync_rsh}" \
    "${LOCAL_STAGE_DIR}/" \
    "${REMOTE_HOST}:${REMOTE_INCOMING}/"
}

diagnose_remote() {
  warn "收集远端非秘密诊断"
  ssh_run bash -s -- \
    "${REMOTE_DIR}" \
    "${REMOTE_INCOMING}" \
    "${COMPOSE_PROJECT}" <<'REMOTE_DIAG' >&2 || true
set -u
remote_dir="$1"
incoming="$2"
project="$3"

candidate=""
if [ -f "${remote_dir}/docker-compose.yaml" ]; then
  candidate="${remote_dir}"
elif [ -f "${incoming}/docker-compose.yaml" ]; then
  candidate="${incoming}"
fi

printf '[remote-diagnostic] project=%s\n' "${project}"
docker ps -a --filter "label=com.docker.compose.project=${project}" \
  --format '  {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' || true
if [ -n "${candidate}" ]; then
  (
    cd -- "${candidate}"
    docker compose ps || true
  )
fi
REMOTE_DIAG
}

activate_remote() {
  log "远端构建、原子安装并启动"
  if ! ssh_run bash -s -- \
    "${REMOTE_INCOMING}" \
    "${REMOTE_DIR}" \
    "${COMPOSE_PROJECT}" \
    "${IMAGE_REF}" \
    "${BUILD_NETWORK}" \
    "${SKIP_BUILD}" \
    "${REPLACE_EXISTING}" \
    "${DEPLOYMENT_ID}" \
    "${HEALTH_TIMEOUT}" \
    "${DASHBOARD_PORT}" <<'REMOTE_DEPLOY'; then
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

incoming="$1"
remote_dir="$2"
project="$3"
image_ref="$4"
build_network="$5"
skip_build="$6"
replace="$7"
deployment_id="$8"
health_timeout="$9"
dashboard_port="${10}"

promoted=0
old_stopped=0

log() {
  printf '[remote-deploy] %s\n' "$*" >&2
}

fail() {
  printf '[remote-deploy] ERROR: %s\n' "$*" >&2
  exit 1
}

read_env_value() {
  file="$1"
  key="$2"
  line="$(
    awk -v wanted="${key}" '
      index($0, wanted "=") == 1 {
        sub(/\r$/, "")
        print
        found = 1
        exit
      }
      END { if (!found) exit 1 }
    ' "${file}"
  )" || return 1
  value="${line#*=}"
  case "${value}" in
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
  esac
  printf '%s' "${value}"
}

show_diagnostics() {
  candidate="$1"
  docker ps -a --filter "label=com.docker.compose.project=${project}" \
    --format '[remote-deploy] {{.Names}}\t{{.Status}}\t{{.Image}}\t{{.Ports}}' \
    >&2 || true
  if [ -d "${candidate}" ] && [ -f "${candidate}/docker-compose.yaml" ]; then
    (
      cd -- "${candidate}"
      docker compose ps >&2 || true
    )
  fi
}

rollback() {
  status="$1"
  trap - EXIT ERR
  show_diagnostics "${remote_dir}"

  if [ "${promoted}" = "1" ]; then
    log "全新部署失败；保留 preflight 实例和目录供诊断：${remote_dir}"
  elif [ "${old_stopped}" = "1" ]; then
    log "旧目标已按要求彻底清除；新部署尚未安装，需要检查上传目录：${incoming}"
  fi
  exit "${status}"
}

finish() {
  status=$?
  trap - EXIT ERR
  if [ "${status}" -ne 0 ]; then
    rollback "${status}"
  fi
  exit 0
}
trap finish EXIT

[ -d "${incoming}" ] || fail "上传目录不存在：${incoming}"
[ ! -L "${incoming}" ] || fail "上传目录不能是符号链接"
cd -- "${incoming}"

chmod 0600 .env secrets/controller-gitlab-token
[ -f .hollysys-source ] || fail "缺少来源标记 .hollysys-source"
[ -f .hollysys-manifest.sha256 ] ||
  fail "缺少 payload manifest"
sha256sum --check --strict .hollysys-manifest.sha256 >/dev/null ||
  fail "上传后的非秘密 payload SHA-256 校验失败"
[ ! -e data/auth.json ] ||
  fail "上传包不得包含本机 data/auth.json"
if [ "${replace}" = "1" ] && [ -e "${remote_dir}/data/auth.json" ]; then
  [ -f "${remote_dir}/data/auth.json" ] ||
    fail "远端模型认证不是普通文件"
  [ ! -L "${remote_dir}/data/auth.json" ] ||
    fail "远端模型认证不能是符号链接"
  install -m 0600 -- "${remote_dir}/data/auth.json" data/auth.json
  log "已在远端内部保留目标实例的模型认证（未输出内容）"
fi
find data/profiles -mindepth 2 -maxdepth 2 -name .env -type f \
  -exec chmod 0600 {} +
chmod 0700 data projects controller-data secrets

docker compose config --quiet
base_image="$(read_env_value .env HERMES_BASE_IMAGE || true)"
if [ -z "${base_image}" ]; then
  base_image="$(
    awk -F= '/^ARG HERMES_BASE_IMAGE=/{print $2; exit}' Dockerfile
  )"
fi
[ -n "${base_image}" ] ||
  fail ".env 和 Dockerfile 都缺少 HERMES_BASE_IMAGE"

if [ "${skip_build}" = "1" ]; then
  docker image inspect "${image_ref}" >/dev/null 2>&1 ||
    fail "远端不存在指定镜像：${image_ref}"
  log "复用已存在镜像：${image_ref}"
else
  log "构建唯一派生镜像：${image_ref}"
  docker build \
    --network="${build_network}" \
    --platform=linux/amd64 \
    --build-arg "HERMES_BASE_IMAGE=${base_image}" \
    --tag "${image_ref}" \
    .
fi

if [ -e "${remote_dir}" ]; then
  if [ "${replace}" = "1" ]; then
    [ -d "${remote_dir}" ] && [ ! -L "${remote_dir}" ] ||
      fail "替换目标必须是普通目录"
    log "停止并彻底清除目标 Compose 项目的容器、卷、网络和目录"
    (
      cd -- "${remote_dir}"
      docker compose down --volumes --remove-orphans
    )
    old_stopped=1
    case "${remote_dir}" in
      /home/*/*|/srv/*/*|/opt/*/*) ;;
      *) fail "拒绝清除过宽的远端目录：${remote_dir}" ;;
    esac
    rm -rf -- "${remote_dir}"
  elif [ -d "${remote_dir}" ] &&
       [ -z "$(find "${remote_dir}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    rmdir -- "${remote_dir}"
  else
    fail "远端目录在预检后变为非空：${remote_dir}"
  fi
fi

mv -- "${incoming}" "${remote_dir}"
promoted=1
cd -- "${remote_dir}"
{
  printf 'deployment_schema=1\n'
  printf 'deployment_id=%s\n' "${deployment_id}"
  printf 'compose_project=%s\n' "${project}"
  printf 'image=%s\n' "${image_ref}"
  printf 'controller_mode=preflight\n'
} >.hollysys-deployment
chmod 0600 .hollysys-deployment
cat .hollysys-source >>.hollysys-deployment

docker compose config --quiet
docker compose up -d --no-build --force-recreate

deadline=$((SECONDS + health_timeout))
while :; do
  all_healthy=1
  for service in controller hermes; do
    container_id="$(docker compose ps -q "${service}")"
    [ -n "${container_id}" ] || {
      all_healthy=0
      continue
    }
    state="$(
      docker inspect --format \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
        "${container_id}"
    )"
    case "${state}" in
      healthy) ;;
      exited|dead|unhealthy)
        fail "${service} 进入失败状态：${state}"
        ;;
      *) all_healthy=0 ;;
    esac
  done
  [ "${all_healthy}" = "1" ] && break
  ((SECONDS < deadline)) || fail "等待容器 healthy 超时（${health_timeout}s）"
  sleep 5
done

log "两个容器均 healthy，在 Controller 容器执行静态 preflight"
docker compose exec -T controller hollysysctl preflight
docker compose exec -T controller hollysysctl health --probe readiness

if docker compose exec -T controller \
    hermes auth status openai-codex >/dev/null 2>&1; then
  log "Fleet 级 openai-codex 认证状态可用"
else
  log "WARNING: openai-codex 尚不可用；需人工登录后重建/重启 Hermes"
fi

if command -v curl >/dev/null 2>&1; then
  http_code="$(
    curl --connect-timeout 5 --max-time 10 \
      --silent --show-error --output /dev/null \
      --write-out '%{http_code}' \
      "http://127.0.0.1:${dashboard_port}/" || true
  )"
  case "${http_code}" in
    200|301|302|401)
      log "Dashboard 本机 HTTP 可达：${http_code}"
      ;;
    *)
      fail "Dashboard 本机 HTTP 检查失败：${http_code:-无状态码}"
      ;;
  esac
fi

docker compose ps
printf 'DEPLOYMENT_OK=1\n'
printf 'DEPLOYMENT_DIR=%s\n' "${remote_dir}"
printf 'DEPLOYMENT_IMAGE=%s\n' "${image_ref}"
printf 'CONTROLLER_MODE=preflight\n'
REMOTE_DEPLOY
    diagnose_remote
    return 1
  fi

  REMOTE_INCOMING=""
}

main() {
  local preflight_output remote_uid remote_gid

  validate_inputs
  validate_local_configuration
  init_ssh
  trap cleanup EXIT INT TERM HUP

  SSH_CONNECTION_OPEN=1
  preflight_output="$(remote_preflight "${REPLACE_EXISTING}")"
  printf '%s\n' "${preflight_output}" >&2
  remote_uid="$(
    printf '%s\n' "${preflight_output}" |
      awk -F= '/^REMOTE_UID=/{print $2; exit}'
  )"
  remote_gid="$(
    printf '%s\n' "${preflight_output}" |
      awk -F= '/^REMOTE_GID=/{print $2; exit}'
  )"
  [[ "${remote_uid}" =~ ^[0-9]+$ && "${remote_gid}" =~ ^[0-9]+$ ]] ||
    die "无法从远端预检获取 UID/GID"

  prepare_local_stage "${remote_uid}" "${remote_gid}"
  confirm_replacement

  log "部署摘要"
  log "  host=${REMOTE_HOST}"
  log "  dir=${REMOTE_DIR}"
  log "  project=${COMPOSE_PROJECT}"
  log "  containers=${HERMES_CONTAINER},${CONTROLLER_CONTAINER}"
  log "  dashboard=${DASHBOARD_PORT}:9119"
  log "  subnet=${DOCKER_SUBNET}"
  log "  image=${IMAGE_REF}"
  log "  controller_mode=preflight"
  log "  replace_existing=${REPLACE_EXISTING}"

  if ((DRY_RUN == 1)); then
    log "DRY RUN 完成：未上传、未构建、未停止或启动任何远端容器"
    return 0
  fi

  upload_stage
  activate_remote

  log "远程部署完成"
  log "后续必须人工完成：真实模型调用、三路飞书 E2E、deep preflight、批准后切换 active"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
