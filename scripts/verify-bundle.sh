#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
profiles=(
  dispatcher prd-writer fde spec-writer spec-reviewer planner
  plan-reviewer tasker task-reviewer coder tester code-reviewer
)

for profile in "${profiles[@]}"; do
  base="${repo_root}/profiles/${profile}"
  test -f "${base}/distribution.yaml"
  test -f "${base}/SOUL.md"
  test -f "${base}/profile.yaml"
  test -f "${base}/config.yaml"
  test -f "${base}/.env.template"
  test -f "${base}/.gitignore"
  test -f "${base}/bootstrap/memories/MEMORY.md"
  test -f "${base}/bootstrap/memories/USER.md"
  test "$(find "${base}/skills" -name SKILL.md -type f | wc -l | tr -d ' ')" -eq 1
  grep -qx '_config_version: 33' "${base}/config.yaml"
  grep -qx 'hermes_requires: ">=0.18.2"' "${base}/distribution.yaml"
done

for script in "${repo_root}"/scripts/*.sh; do
  test -x "${script}"
  bash -n "${script}"
done

python3 - "${repo_root}" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
for path in root.rglob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))
print("bundle structure and JSON: ok")
PY

python3 - "${repo_root}" <<'PY'
import pathlib
import re
import sys
import urllib.parse

root = pathlib.Path(sys.argv[1])
missing = []
pattern = re.compile(r"\[[^]]*\]\(([^)]+)\)")
for source in root.rglob("*.md"):
    for raw_target in pattern.findall(source.read_text(encoding="utf-8")):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = urllib.parse.unquote(target.split("#", 1)[0])
        if path_part and not (source.parent / path_part).resolve().exists():
            missing.append(f"{source.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("missing local Markdown targets:\n" + "\n".join(missing))
print("shell syntax and local Markdown links: ok")
PY

if python3 -c 'import yaml' >/dev/null 2>&1; then
  python3 - "${repo_root}" <<'PY'
import pathlib
import sys
import yaml

for path in pathlib.Path(sys.argv[1]).rglob("*.yaml"):
    yaml.safe_load(path.read_text(encoding="utf-8"))
print("YAML: ok (PyYAML)")
PY
elif command -v ruby >/dev/null 2>&1; then
  find "${repo_root}" -name '*.yaml' -type f -print0 | \
    xargs -0 ruby -e 'require "yaml"; ARGV.each { |path| YAML.safe_load(File.read(path), permitted_classes: [], aliases: false) }'
  echo "YAML: ok (Ruby)"
else
  echo "YAML parser unavailable; build-time validation still required" >&2
fi

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  FLEET_ENV_FILE=.env.example docker compose \
    --env-file "${repo_root}/.env.example" \
    -f "${repo_root}/docker-compose.yml" config --quiet
  echo "Docker Compose config: ok"
fi
