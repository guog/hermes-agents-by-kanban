#!/usr/bin/env bash

set -euo pipefail

# Deployment assets are intentionally not committed to this repository.
# This script downloads immutable, reviewed upstream revisions into the
# repo-local skills/ and cli/ directories before Docker Compose is started.

GLAB_VERSION="1.108.0"
GLAB_URL="https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.tar.gz"
GLAB_BINARY_SHA256="4be20481e18489e59c966206df09e3c1f61988a1d5bab76d2490f67312cd6e7f"

LARK_CLI_VERSION="1.0.72"
LARK_CLI_URL="https://github.com/larksuite/cli/releases/download/v${LARK_CLI_VERSION}/lark-cli-${LARK_CLI_VERSION}-linux-amd64.tar.gz"
LARK_CLI_ARCHIVE_SHA256="395e6b7e99e106afe5de77750f65b6d40421c8fec8e885b7a971cdf7c5d39d02"
LARK_CLI_BINARY_SHA256="0ef7e817c4bf5f3cdaf12a15169c7a076df3af63c6cd67e3c793dd159ec27305"

GITLAB_SKILLS_REPOSITORY="https://gitlab.com/gitlab-org/ai/skills.git"
GITLAB_SKILLS_REVISION="933cee89fbbec511241cae0914e5112feda29ab2"

LARK_SKILLS_REPOSITORY="https://github.com/larksuite/cli.git"
LARK_SKILLS_REVISION="d6cebd6723eb80e9e5761d34ccea9ab71e2f5a8d"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CLI_DIR="${REPO_ROOT}/cli"
SKILLS_DIR="${REPO_ROOT}/skills"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/hermes-external-assets.XXXXXX")"
STAGE_DIR="${WORK_DIR}/stage"
SWAPPING=0
CLI_ORIGINAL_PRESENT=0
SKILLS_ORIGINAL_PRESENT=0

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '错误：缺少命令 %s。\n' "$1" >&2
    exit 1
  fi
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    printf '错误：需要 sha256sum 或 shasum。\n' >&2
    exit 1
  fi
}

verify_sha256() {
  local file="$1"
  local expected="$2"
  local actual
  actual="$(sha256_file "${file}")"
  if [[ "${actual}" != "${expected}" ]]; then
    printf '错误：SHA-256 不匹配：%s\n期望：%s\n实际：%s\n' \
      "${file}" "${expected}" "${actual}" >&2
    exit 1
  fi
}

download() {
  local url="$1"
  local destination="$2"
  printf '下载 %s\n' "${url}"
  curl --fail --location --silent --show-error \
    --retry 3 --retry-delay 2 \
    --output "${destination}" "${url}"
}

clone_at_revision() {
  local repository="$1"
  local revision="$2"
  local destination="$3"
  local actual

  git init --quiet "${destination}"
  git -C "${destination}" remote add origin "${repository}"
  git -C "${destination}" fetch --quiet --depth 1 origin "${revision}"
  actual="$(git -C "${destination}" rev-parse FETCH_HEAD)"
  if [[ "${actual}" != "${revision}" ]]; then
    printf '错误：上游 revision 不匹配：%s（期望 %s，实际 %s）。\n' \
      "${repository}" "${revision}" "${actual}" >&2
    exit 1
  fi
  git -C "${destination}" checkout --quiet --detach FETCH_HEAD
}

find_single_binary() {
  local search_root="$1"
  local binary_name="$2"
  local matches
  local count

  matches="$(find "${search_root}" -type f -name "${binary_name}" -print)"
  count="$(printf '%s\n' "${matches}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "${count}" != "1" ]]; then
    printf '错误：在 %s 中找到 %s 个 %s，预期恰好一个。\n' \
      "${search_root}" "${count}" "${binary_name}" >&2
    exit 1
  fi
  printf '%s\n' "${matches}"
}

rollback_swap() {
  local failed_cli="${WORK_DIR}/failed-cli"
  local failed_skills="${WORK_DIR}/failed-skills"

  if [[ -e "${WORK_DIR}/old-cli" || -L "${WORK_DIR}/old-cli" ]]; then
    if [[ -e "${CLI_DIR}" || -L "${CLI_DIR}" ]]; then
      mv "${CLI_DIR}" "${failed_cli}"
    fi
    mv "${WORK_DIR}/old-cli" "${CLI_DIR}"
  elif [[ "${CLI_ORIGINAL_PRESENT}" == "0" && ( -e "${CLI_DIR}" || -L "${CLI_DIR}" ) ]]; then
    mv "${CLI_DIR}" "${failed_cli}"
  fi
  if [[ -e "${WORK_DIR}/old-skills" || -L "${WORK_DIR}/old-skills" ]]; then
    if [[ -e "${SKILLS_DIR}" || -L "${SKILLS_DIR}" ]]; then
      mv "${SKILLS_DIR}" "${failed_skills}"
    fi
    mv "${WORK_DIR}/old-skills" "${SKILLS_DIR}"
  elif [[ "${SKILLS_ORIGINAL_PRESENT}" == "0" && ( -e "${SKILLS_DIR}" || -L "${SKILLS_DIR}" ) ]]; then
    mv "${SKILLS_DIR}" "${failed_skills}"
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  if [[ "${SWAPPING}" == "1" && "${status}" != "0" ]]; then
    rollback_swap
  fi
  rm -rf "${WORK_DIR}"
  exit "${status}"
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -f "${REPO_ROOT}/docker-compose.yaml" || ! -d "${REPO_ROOT}/.git" ]]; then
  printf '错误：脚本必须位于 hermes-agents-by-kanban Git 仓库的 scripts/ 目录。\n' >&2
  exit 1
fi

for command_name in curl git tar find awk sed wc tr install; do
  require_command "${command_name}"
done

mkdir -p "${STAGE_DIR}/cli/bin" "${STAGE_DIR}/cli/licenses"
mkdir -p "${STAGE_DIR}/skills/gitlab" "${STAGE_DIR}/skills/lark"

GLAB_ARCHIVE="${WORK_DIR}/glab.tar.gz"
GLAB_EXTRACT="${WORK_DIR}/glab"
mkdir -p "${GLAB_EXTRACT}"
download "${GLAB_URL}" "${GLAB_ARCHIVE}"
tar -xzf "${GLAB_ARCHIVE}" -C "${GLAB_EXTRACT}"
GLAB_BINARY="$(find_single_binary "${GLAB_EXTRACT}" glab)"
verify_sha256 "${GLAB_BINARY}" "${GLAB_BINARY_SHA256}"
install -m 0755 "${GLAB_BINARY}" "${STAGE_DIR}/cli/bin/glab"
download \
  "https://gitlab.com/gitlab-org/cli/-/raw/v${GLAB_VERSION}/LICENSE" \
  "${STAGE_DIR}/cli/licenses/glab-LICENSE"

LARK_ARCHIVE="${WORK_DIR}/lark-cli.tar.gz"
LARK_EXTRACT="${WORK_DIR}/lark-cli"
mkdir -p "${LARK_EXTRACT}"
download "${LARK_CLI_URL}" "${LARK_ARCHIVE}"
verify_sha256 "${LARK_ARCHIVE}" "${LARK_CLI_ARCHIVE_SHA256}"
tar -xzf "${LARK_ARCHIVE}" -C "${LARK_EXTRACT}"
LARK_BINARY="$(find_single_binary "${LARK_EXTRACT}" lark-cli)"
verify_sha256 "${LARK_BINARY}" "${LARK_CLI_BINARY_SHA256}"
install -m 0755 "${LARK_BINARY}" "${STAGE_DIR}/cli/bin/lark-cli"

GITLAB_SKILLS_CHECKOUT="${WORK_DIR}/gitlab-skills"
clone_at_revision \
  "${GITLAB_SKILLS_REPOSITORY}" \
  "${GITLAB_SKILLS_REVISION}" \
  "${GITLAB_SKILLS_CHECKOUT}"
cp -R "${GITLAB_SKILLS_CHECKOUT}/skills/glab" "${STAGE_DIR}/skills/gitlab/"
cp "${GITLAB_SKILLS_CHECKOUT}/LICENSE" "${STAGE_DIR}/skills/gitlab/LICENSE"

LARK_SKILLS_CHECKOUT="${WORK_DIR}/lark-skills"
clone_at_revision \
  "${LARK_SKILLS_REPOSITORY}" \
  "${LARK_SKILLS_REVISION}" \
  "${LARK_SKILLS_CHECKOUT}"
cp -R "${LARK_SKILLS_CHECKOUT}/skills/lark-shared" "${STAGE_DIR}/skills/lark/"
cp -R "${LARK_SKILLS_CHECKOUT}/skills/lark-im" "${STAGE_DIR}/skills/lark/"
cp "${LARK_SKILLS_CHECKOUT}/LICENSE" "${STAGE_DIR}/skills/lark/LICENSE"
cp "${LARK_SKILLS_CHECKOUT}/LICENSE" "${STAGE_DIR}/cli/licenses/lark-cli-LICENSE"

test -x "${STAGE_DIR}/cli/bin/glab"
test -x "${STAGE_DIR}/cli/bin/lark-cli"
test -f "${STAGE_DIR}/skills/gitlab/glab/SKILL.md"
test -f "${STAGE_DIR}/skills/lark/lark-shared/SKILL.md"
test -f "${STAGE_DIR}/skills/lark/lark-im/SKILL.md"

cat >"${STAGE_DIR}/cli/MANIFEST.txt" <<EOF
platform=linux-amd64
glab.version=${GLAB_VERSION}
glab.source=${GLAB_URL}
glab.binary_sha256=${GLAB_BINARY_SHA256}
lark-cli.version=${LARK_CLI_VERSION}
lark-cli.source=${LARK_CLI_URL}
lark-cli.archive_sha256=${LARK_CLI_ARCHIVE_SHA256}
lark-cli.binary_sha256=${LARK_CLI_BINARY_SHA256}
EOF

cat >"${STAGE_DIR}/skills/MANIFEST.txt" <<EOF
gitlab.repository=${GITLAB_SKILLS_REPOSITORY}
gitlab.revision=${GITLAB_SKILLS_REVISION}
gitlab.paths=skills/glab
lark.repository=${LARK_SKILLS_REPOSITORY}
lark.revision=${LARK_SKILLS_REVISION}
lark.paths=skills/lark-shared,skills/lark-im
EOF

SWAPPING=1
if [[ -e "${CLI_DIR}" || -L "${CLI_DIR}" ]]; then
  CLI_ORIGINAL_PRESENT=1
  mv "${CLI_DIR}" "${WORK_DIR}/old-cli"
fi
if [[ -e "${SKILLS_DIR}" || -L "${SKILLS_DIR}" ]]; then
  SKILLS_ORIGINAL_PRESENT=1
  mv "${SKILLS_DIR}" "${WORK_DIR}/old-skills"
fi
mv "${STAGE_DIR}/cli" "${CLI_DIR}"
mv "${STAGE_DIR}/skills" "${SKILLS_DIR}"
SWAPPING=0

printf '\n下载与校验完成：\n'
printf '  CLI:    %s\n' "${CLI_DIR}"
printf '  Skills: %s\n' "${SKILLS_DIR}"
printf '这些目录已被 .gitignore 排除；部署时由 Compose 只读挂载。\n'
