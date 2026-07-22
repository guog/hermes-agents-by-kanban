#!/usr/bin/env bash
set -euo pipefail
set +x

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
env_file="${repo_root}/.env"
expected_image="nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40"

fail() {
  echo "deployment env initialization: $*" >&2
  exit 1
}

for command in docker git openssl python3; do
  command -v "${command}" >/dev/null 2>&1 || fail "required command is unavailable: ${command}"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is unavailable"
[[ -t 0 ]] || fail "run this script interactively from a terminal"

if [[ -e "${env_file}" || -L "${env_file}" ]]; then
  [[ -f "${env_file}" && ! -L "${env_file}" ]] || fail "root .env must be a regular file, not a symbolic link"
else
  install -m 0600 "${repo_root}/.env.example" "${env_file}"
fi
chmod 0600 "${env_file}"

[[ -z $(git -C "${repo_root}" status --porcelain) ]] || fail "repository must be clean before locking a deployment ref"
git -C "${repo_root}" diff --check
git -C "${repo_root}" fsck --full
bundle_ref=$(git -C "${repo_root}" rev-parse HEAD)
[[ "${bundle_ref}" =~ ^[0-9a-f]{40}$ ]] || fail "could not resolve one exact deployment commit"

docker image inspect "${expected_image}" >/dev/null 2>&1 || \
  fail "locked Hermes AMD64 image is not available locally"

dashboard_password=""
dashboard_password_confirm=""
password_hash=""
signing_secret=""
trap 'unset dashboard_password dashboard_password_confirm password_hash signing_secret' EXIT

read -r -s -p "Dashboard initial password: " dashboard_password
printf '\n'
read -r -s -p "Confirm dashboard initial password: " dashboard_password_confirm
printf '\n'
[[ -n "${dashboard_password}" ]] || fail "dashboard password must not be empty"
[[ ${#dashboard_password} -ge 12 ]] || fail "dashboard password must contain at least 12 characters"
[[ "${dashboard_password}" == "${dashboard_password_confirm}" ]] || fail "dashboard passwords do not match"
unset dashboard_password_confirm

password_hash=$(
  printf '%s\n' "${dashboard_password}" | docker run --rm -i --pull=never \
    --workdir /opt/hermes \
    --entrypoint /opt/hermes/.venv/bin/python \
    "${expected_image}" \
    -c 'import sys; from plugins.dashboard_auth.basic import hash_password; password = sys.stdin.readline().rstrip("\n"); print(hash_password(password))'
)
unset dashboard_password
[[ "${password_hash}" == scrypt\$16384\$8\$1\$* ]] || fail "Hermes returned an unexpected password hash"
signing_secret=$(openssl rand -base64 32 | tr -d '\n')

runtime_uid=$(id -u)
runtime_gid=$(id -g)
python3 - "${env_file}" 3<<<"${bundle_ref}
${runtime_uid}
${runtime_gid}
${password_hash}
${signing_secret}" <<'PY'
import os
import pathlib
import re
import stat
import sys
import tempfile

path = pathlib.Path(sys.argv[1])
status = path.lstat()
if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
    raise SystemExit("root .env must remain a regular file")

payload = os.fdopen(3, encoding="utf-8").read().splitlines()
if len(payload) != 5:
    raise SystemExit("internal secret transfer failed")
bundle_ref, runtime_uid, runtime_gid, password_hash, signing_secret = payload
updates = {
    "HERMES_IMAGE": "nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40",
    "FLEET_BUNDLE_REF": bundle_ref,
    "PUID": runtime_uid,
    "PGID": runtime_gid,
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "admin",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH": "'" + password_hash + "'",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "'" + signing_secret + "'",
    "FLEET_FORCE_CONFIG": "0",
}
key_re = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
seen = set()
output = []
for line in path.read_text(encoding="utf-8").splitlines():
    match = key_re.match(line)
    key = match.group(1) if match else None
    if key == "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD":
        continue
    if key in updates:
        if key in seen:
            raise SystemExit(f"duplicate root .env variable: {key}")
        output.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        output.append(line)
for key, value in updates.items():
    if key not in seen:
        output.append(f"{key}={value}")

fd, temporary_name = tempfile.mkstemp(prefix=".env.", dir=path.parent, text=True)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("\n".join(output) + "\n")
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY

unset password_hash signing_secret
"${repo_root}/scripts/validate-deployment-env.py" \
  --env-file "${env_file}"
echo "deployment env initialization: root .env created with hashed dashboard authentication"
