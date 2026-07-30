#!/bin/sh
set -eu

workspace=${1:-}
case "$workspace" in
  /workspace/projects/*) ;;
  *)
    printf '%s\n' '{"ok":false,"error_code":"unsafe_workspace"}'
    exit 64
    ;;
esac

if [ ! -d "$workspace" ]; then
  printf '%s\n' '{"ok":false,"error_code":"workspace_missing"}'
  exit 66
fi

npm_cache=${NPM_CONFIG_CACHE:-/opt/data/cache/npm}
nuget_cache=${NUGET_PACKAGES:-/opt/data/cache/nuget}
mkdir -p "$npm_cache" "$nuget_cache"

npm_projects=0
dotnet_projects=0

find "$workspace" -type f -name package-lock.json -not -path '*/node_modules/*' \
  -print | sort | while IFS= read -r lockfile; do
    package_dir=$(dirname "$lockfile")
    (
      cd "$package_dir"
      NPM_CONFIG_OFFLINE=false \
      NPM_CONFIG_CACHE="$npm_cache" \
      npm ci --no-audit --no-fund
    )
  done
npm_projects=$(find "$workspace" -type f -name package-lock.json \
  -not -path '*/node_modules/*' | wc -l | tr -d ' ')

find "$workspace" -type f \( -name '*.sln' -o -name '*.slnx' \) \
  -print | sort | while IFS= read -r solution; do
    NUGET_PACKAGES="$nuget_cache" dotnet restore "$solution"
  done
dotnet_projects=$(find "$workspace" -type f \
  \( -name '*.sln' -o -name '*.slnx' \) | wc -l | tr -d ' ')

printf '{"ok":true,"npm_projects":%s,"dotnet_projects":%s}\n' \
  "$npm_projects" "$dotnet_projects"

