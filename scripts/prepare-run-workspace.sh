#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 8 ]]; then
  echo "usage: $0 <project-id> <path-with-namespace> <https-clone-url> <repo-slug> <run-key> <branch> <base-sha> <display-name>" >&2
  exit 2
fi

project_id=$1
project_path=$2
clone_url=$3
repo_slug=$4
run_key=$5
branch=$6
base_sha=$7
display_name=$8

if [[ ! "${project_id}" =~ ^[1-9][0-9]*$ ]]; then
  echo "project id must be a positive integer" >&2
  exit 2
fi
if [[ ! "${project_path}" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$ ]]; then
  echo "invalid path_with_namespace" >&2
  exit 2
fi
if [[ ! "${repo_slug}" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "repo slug must be lowercase and filesystem-safe" >&2
  exit 2
fi
if [[ ! "${run_key}" =~ ^sdd-[a-z2-7]{20}$ ]]; then
  echo "invalid run_key" >&2
  exit 2
fi
if [[ ! "${base_sha}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "base sha must be a full lowercase commit SHA" >&2
  exit 2
fi
git check-ref-format --branch "${branch}" >/dev/null

host=${GITLAB_HOST#https://}
host=${host#http://}
host=${host%/}
if [[ "${clone_url}" != "https://${host}/${project_path}" && \
      "${clone_url}" != "https://${host}/${project_path}.git" ]]; then
  echo "clone URL must be token-free HTTPS for the validated project" >&2
  exit 2
fi

allowed=0
IFS=',' read -r -a allowed_groups <<< "${GITLAB_ALLOWED_GROUPS:-}"
for group in "${allowed_groups[@]}"; do
  group=${group#"${group%%[![:space:]]*}"}
  group=${group%"${group##*[![:space:]]}"}
  if [[ -n "${group}" && "${project_path}" == "${group}/"* ]]; then
    allowed=1
    break
  fi
done
if [[ ${allowed} -ne 1 ]]; then
  echo "project is outside GITLAB_ALLOWED_GROUPS" >&2
  exit 3
fi

checkout="/workspace/projects/p${project_id}-${repo_slug}"
board="gitlab-p${project_id}"
worktree="/workspace/projects/worktrees/p${project_id}/${run_key}"

mkdir -p /workspace/projects /workspace/projects/worktrees/"p${project_id}"
if [[ ! -e "${checkout}" ]]; then
  git clone "${clone_url}" "${checkout}"
elif [[ ! -d "${checkout}/.git" ]]; then
  echo "checkout path exists but is not a Git repository: ${checkout}" >&2
  exit 4
fi

origin=$(git -C "${checkout}" remote get-url origin)
if [[ "${origin}" != "https://${host}/${project_path}" && \
      "${origin}" != "https://${host}/${project_path}.git" ]]; then
  echo "existing checkout origin does not match project identity" >&2
  exit 4
fi
git -C "${checkout}" fetch --prune origin
git -C "${checkout}" cat-file -e "${base_sha}^{commit}"

if [[ -d "${worktree}/.git" || -f "${worktree}/.git" ]]; then
  actual_branch=$(git -C "${worktree}" branch --show-current)
  if [[ "${actual_branch}" != "${branch}" ]]; then
    echo "existing run worktree uses branch ${actual_branch}, expected ${branch}" >&2
    exit 4
  fi
else
  if git -C "${checkout}" show-ref --verify --quiet "refs/heads/${branch}"; then
    git -C "${checkout}" worktree add "${worktree}" "${branch}"
  elif git -C "${checkout}" show-ref --verify --quiet "refs/remotes/origin/${branch}"; then
    git -C "${checkout}" worktree add --track -b "${branch}" "${worktree}" "origin/${branch}"
  else
    git -C "${checkout}" worktree add -b "${branch}" "${worktree}" "${base_sha}"
  fi
fi

hermes -p dispatcher kanban boards create "${board}" \
  --name "${display_name}" \
  --description "PRD delivery for ${project_path}" \
  --default-workdir "${checkout}" >/dev/null

printf 'project_id=%s\nproject_path=%s\ncheckout=%s\nworktree=%s\nboard=%s\nbranch=%s\n' \
  "${project_id}" "${project_path}" "${checkout}" "${worktree}" "${board}" "${branch}"
