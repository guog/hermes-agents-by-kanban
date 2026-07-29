#!/command/with-contenv sh
set -eu

install -d -o root -g root -m 0755 /run/hollysys/bin
install -d -o root -g root -m 0755 /usr/local/bin

for executable in git gitlab-askpass gitlab-credential; do
  install \
    -o root \
    -g root \
    -m 0555 \
    "/opt/fleet/container/git/$executable" \
    "/run/hollysys/bin/$executable"
  install \
    -o root \
    -g root \
    -m 0555 \
    "/opt/fleet/container/git/$executable" \
    "/usr/local/bin/$executable"
done

for executable in glab lark-cli; do
  source="/opt/cli/bin/$executable"
  if [ ! -x "$source" ]; then
    echo "hollysys-cli-install error=locked_cli_missing executable=$executable" >&2
    exit 66
  fi
  install \
    -o root \
    -g root \
    -m 0555 \
    "$source" \
    "/usr/local/bin/$executable"
done
