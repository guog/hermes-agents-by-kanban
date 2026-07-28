#!/command/with-contenv sh
set -eu

log() {
  printf '%s\n' "[container-init] $*"
}

has_dotnet8() {
  command -v dotnet >/dev/null 2>&1 \
    && dotnet --list-sdks 2>/dev/null | grep -Eq '^8[.]'
}

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    log "ERROR: installing .NET SDK 8 requires root or sudo"
    return 1
  fi
}

if has_dotnet8; then
  log ".NET SDK 8 already exists; installation skipped"
  dotnet --list-sdks
  exit 0
fi

if [ ! -r /etc/os-release ]; then
  log "ERROR: cannot verify the container operating system"
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [ "${ID:-}" != "debian" ] || [ "${VERSION_ID:-}" != "13" ]; then
  log "ERROR: the mounted installer supports Debian 13 only (found ${ID:-unknown} ${VERSION_ID:-unknown})"
  exit 1
fi

work_dir=$(mktemp -d)
repo_package="${work_dir}/packages-microsoft-prod.deb"
repo_added=0

cleanup() {
  if [ "${repo_added}" -eq 1 ]; then
    run_as_root env DEBIAN_FRONTEND=noninteractive \
      dpkg --purge packages-microsoft-prod >/dev/null 2>&1 || true
  fi
  rm -rf "${work_dir}"
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM

log ".NET SDK 8 is missing; installing it for Debian 13"
# Remove a partial repository setup left by an interrupted prior start before
# the first apt update, so the steady-state source set remains deterministic.
run_as_root env DEBIAN_FRONTEND=noninteractive \
  dpkg --purge packages-microsoft-prod >/dev/null 2>&1 || true
run_as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get update
run_as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y --no-install-recommends ca-certificates wget

wget \
  https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb \
  -O "${repo_package}"

run_as_root env DEBIAN_FRONTEND=noninteractive \
  dpkg -i "${repo_package}"
repo_added=1

run_as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get update
run_as_root env DEBIAN_FRONTEND=noninteractive \
  apt-get install -y --no-install-recommends dotnet-sdk-8.0

if ! has_dotnet8; then
  log "ERROR: installation completed but .NET SDK 8 is still unavailable"
  exit 1
fi

# The Microsoft feed is needed only for the SDK bootstrap. Remove it so later
# apt/apt-get operations use only the mounted Alibaba Debian mirrors.
run_as_root env DEBIAN_FRONTEND=noninteractive \
  dpkg --purge packages-microsoft-prod >/dev/null
repo_added=0
run_as_root apt-get clean

log ".NET SDK 8 installation verified"
dotnet --list-sdks
