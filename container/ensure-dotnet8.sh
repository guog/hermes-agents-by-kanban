#!/command/with-contenv sh
set -eu

PERSISTENT_DOTNET_ROOT=/opt/data/.dotnet

log() {
  printf '%s\n' "[container-init] $*"
}

has_dotnet8_at() {
  dotnet_root=$1
  [ -x "$dotnet_root/dotnet" ] \
    && "$dotnet_root/dotnet" --list-sdks 2>/dev/null | grep -Eq '^8[.]'
}

current_dotnet_root() {
  dotnet_command=$(command -v dotnet 2>/dev/null || true)
  [ -n "$dotnet_command" ] || return 1
  resolved_dotnet=$(readlink -f "$dotnet_command" 2>/dev/null || true)
  [ -n "$resolved_dotnet" ] || return 1
  dirname "$resolved_dotnet"
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

remove_microsoft_feed() {
  run_as_root env DEBIAN_FRONTEND=noninteractive \
    dpkg --purge packages-microsoft-prod >/dev/null 2>&1 || true
  if [ -d /var/lib/apt/lists ]; then
    run_as_root find /var/lib/apt/lists \
      -type f -name '*packages.microsoft.com*' -delete
  fi
}

work_dir=
persistent_stage=
repo_added=0

cleanup() {
  if [ "$repo_added" -eq 1 ]; then
    remove_microsoft_feed
  fi
  if [ -n "$work_dir" ]; then
    rm -rf "$work_dir"
  fi
  if [ -n "$persistent_stage" ]; then
    rm -rf "$persistent_stage"
  fi
}
trap cleanup EXIT
trap 'exit 143' HUP INT TERM

persist_dotnet8() {
  source_root=$(current_dotnet_root) || {
    log "ERROR: cannot locate the installed .NET root"
    return 1
  }
  if [ "$source_root" = "$PERSISTENT_DOTNET_ROOT" ]; then
    return 0
  fi
  if [ -e "$PERSISTENT_DOTNET_ROOT" ]; then
    log "ERROR: persistent .NET root exists but does not contain a valid SDK 8"
    return 1
  fi
  if [ ! -d /opt/data ] || [ ! -w /opt/data ]; then
    log "ERROR: /opt/data is not a writable persistent runtime directory"
    return 1
  fi
  persistent_stage=$(mktemp -d /opt/data/.dotnet-stage.XXXXXX)
  cp -a "$source_root/." "$persistent_stage/"
  chmod -R a+rX,go-w "$persistent_stage"
  if ! has_dotnet8_at "$persistent_stage"; then
    log "ERROR: staged persistent .NET SDK 8 verification failed"
    return 1
  fi
  mv "$persistent_stage" "$PERSISTENT_DOTNET_ROOT"
  persistent_stage=
}

# Always repair a feed left by an interrupted earlier start before deciding
# whether the already-installed SDK allows this start to skip installation.
remove_microsoft_feed

if has_dotnet8_at "$PERSISTENT_DOTNET_ROOT"; then
  log "persistent .NET SDK 8 already exists; installation skipped"
  "$PERSISTENT_DOTNET_ROOT/dotnet" --list-sdks
  exit 0
fi

if current_root=$(current_dotnet_root) \
  && has_dotnet8_at "$current_root"; then
  persist_dotnet8
  log "existing .NET SDK 8 copied to persistent runtime data"
  "$PERSISTENT_DOTNET_ROOT/dotnet" --list-sdks
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

log ".NET SDK 8 is missing; installing it for Debian 13"
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

installed_root=$(current_dotnet_root || true)
if [ -z "$installed_root" ] || ! has_dotnet8_at "$installed_root"; then
  log "ERROR: installation completed but .NET SDK 8 is still unavailable"
  exit 1
fi
persist_dotnet8

# The Microsoft feed is needed only for the SDK bootstrap. Remove it so later
# apt/apt-get operations use only the mounted Alibaba Debian mirrors.
remove_microsoft_feed
repo_added=0
run_as_root apt-get clean

log ".NET SDK 8 installation verified and persisted"
"$PERSISTENT_DOTNET_ROOT/dotnet" --list-sdks
