#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
  echo "usage: $0 <project-id> <repo-slug> <run-key>" >&2
  exit 2
fi

project_id=$1
repo_slug=$2
run_key=$3
if [[ ! "${project_id}" =~ ^[1-9][0-9]*$ || \
      ! "${repo_slug}" =~ ^[a-z0-9][a-z0-9._-]*$ || \
      ! "${run_key}" =~ ^sdd-[a-z2-7]{20}$ ]]; then
  echo "invalid cleanup target" >&2
  exit 2
fi

checkout="/workspace/projects/p${project_id}-${repo_slug}"
worktree="/workspace/projects/worktrees/p${project_id}/${run_key}"
if [[ ! -d "${checkout}/.git" ]]; then
  echo "project checkout is missing: ${checkout}" >&2
  exit 4
fi
if [[ -e "${worktree}" ]]; then
  git -C "${checkout}" worktree remove "${worktree}"
fi
git -C "${checkout}" worktree prune
