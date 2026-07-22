#!/usr/bin/env bash
set -euo pipefail
umask 022

vendor_root=/opt/fleet/vendor
lock_file=/opt/fleet/config/skills-lock.yaml
releases_root="${vendor_root}/releases"

read_lock_value() {
  local section=$1 key=$2
  awk -v wanted_section="${section}" -v wanted_key="${key}" '
    /^  [a-z0-9_]+:$/ {
      current = $1
      sub(/:$/, "", current)
    }
    current == wanted_section && $1 == wanted_key ":" {
      value = $2
      gsub(/^"|"$/, "", value)
      print value
      exit
    }
  ' "${lock_file}"
}

gitlab_skills_rev=$(read_lock_value gitlab_official_skills revision)
glab_version=$(read_lock_value gitlab_official_skills cli_version)
lark_skills_rev=$(read_lock_value lark_cli_official_skills revision)
lark_cli_version=$(read_lock_value lark_cli_official_skills cli_version)

if [[ ! "${glab_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || \
      ! "${lark_cli_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ || \
      ! "${gitlab_skills_rev}" =~ ^[0-9a-f]{40}$ || \
      ! "${lark_skills_rev}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "tooling sync: invalid or incomplete config/skills-lock.yaml" >&2
  exit 67
fi

case "$(uname -m)" in
  x86_64) glab_arch=amd64 ;;
  aarch64|arm64) glab_arch=arm64 ;;
  *) echo "tooling sync: unsupported architecture $(uname -m)" >&2; exit 67 ;;
esac

lock_material="glab=${glab_version};gitlab=${gitlab_skills_rev};lark-cli=${lark_cli_version};lark=${lark_skills_rev};arch=${glab_arch}"
release_key=$(printf '%s' "${lock_material}" | sha256sum | cut -c1-20)
release_name="tooling-${release_key}"
release_dir="${releases_root}/${release_name}"
current_link="${vendor_root}/current"

verify_release() {
  local dir=$1
  test -x "${dir}/bin/glab"
  test -x "${dir}/bin/lark-cli"
  test -f "${dir}/gitlab/skills/glab/SKILL.md"
  test -f "${dir}/lark/skills/lark-shared/SKILL.md"
  test -f "${dir}/lark/skills/lark-im/SKILL.md"
  grep -qxF "${lock_material}" "${dir}/.tooling-lock"
  "${dir}/bin/glab" --version >/dev/null
  "${dir}/bin/lark-cli" --version >/dev/null
}

install -d -m 0755 "${vendor_root}" "${releases_root}"
if [[ -d "${release_dir}" ]] && verify_release "${release_dir}"; then
  link_tmp="${vendor_root}/.current-${release_key}"
  rm -f "${link_tmp}"
  ln -s "releases/${release_name}" "${link_tmp}"
  mv -Tf "${link_tmp}" "${current_link}"
  echo "tooling sync: ${release_name} already present"
  exit 0
fi

if [[ -e "${release_dir}" ]]; then
  rm -rf "${release_dir}"
fi
stage=$(mktemp -d "${vendor_root}/.staging.XXXXXX")
cleanup() {
  if [[ -n "${stage:-}" && -d "${stage}" ]]; then
    rm -rf "${stage}"
  fi
}
trap cleanup EXIT
install -d -m 0755 "${stage}/bin" "${stage}/gitlab" "${stage}/lark"

archive="glab_${glab_version}_linux_${glab_arch}.tar.gz"
curl -fsSL --retry 3 \
  "https://gitlab.com/gitlab-org/cli/-/releases/v${glab_version}/downloads/${archive}" \
  -o "${stage}/${archive}"
curl -fsSL --retry 3 \
  "https://gitlab.com/gitlab-org/cli/-/releases/v${glab_version}/downloads/checksums.txt" \
  -o "${stage}/checksums.txt"
expected_sha=$(awk -v archive="${archive}" '$2 == archive {print $1; exit}' "${stage}/checksums.txt")
if [[ ! "${expected_sha}" =~ ^[0-9a-f]{64}$ ]]; then
  echo "tooling sync: checksum for ${archive} not found" >&2
  exit 68
fi
printf '%s  %s\n' "${expected_sha}" "${stage}/${archive}" | sha256sum -c - >/dev/null
tar -xzf "${stage}/${archive}" -C "${stage}" bin/glab
install -m 0755 "${stage}/bin/glab" "${stage}/bin/glab.verified"
mv "${stage}/bin/glab.verified" "${stage}/bin/glab"
rm "${stage}/${archive}" "${stage}/checksums.txt"

npm install --no-audit --no-fund --prefix "${stage}/lark-cli" \
  "@larksuite/cli@${lark_cli_version}" >/dev/null
test -x "${stage}/lark-cli/node_modules/.bin/lark-cli"
ln -s ../lark-cli/node_modules/.bin/lark-cli "${stage}/bin/lark-cli"

git clone --quiet --filter=blob:none --no-checkout \
  https://gitlab.com/gitlab-org/ai/skills.git "${stage}/gitlab-source"
git -C "${stage}/gitlab-source" checkout --quiet "${gitlab_skills_rev}" -- skills/glab
mv "${stage}/gitlab-source/skills" "${stage}/gitlab/skills"
rm -rf "${stage}/gitlab-source"

git clone --quiet --filter=blob:none --no-checkout \
  https://github.com/larksuite/cli.git "${stage}/lark-source"
git -C "${stage}/lark-source" checkout --quiet "${lark_skills_rev}" -- \
  skills/lark-shared skills/lark-im
mv "${stage}/lark-source/skills" "${stage}/lark/skills"
rm -rf "${stage}/lark-source"

printf '%s\n' "${lock_material}" > "${stage}/.tooling-lock"
chmod -R a+rX,go-w "${stage}"
verify_release "${stage}"

mv "${stage}" "${release_dir}"
stage=
link_tmp="${vendor_root}/.current-${release_key}"
rm -f "${link_tmp}"
ln -s "releases/${release_name}" "${link_tmp}"
mv -Tf "${link_tmp}" "${current_link}"
trap - EXIT
echo "tooling sync: installed ${release_name}"
