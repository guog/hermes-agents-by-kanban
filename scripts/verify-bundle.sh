#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

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
  test ! -e "${base}/.gitignore"
  test -f "${base}/bootstrap/memories/MEMORY.md"
  test -f "${base}/bootstrap/memories/USER.md"
  for seed in MEMORY.md USER.md; do
    if git -C "${repo_root}" check-ignore -q \
      "profiles/${profile}/bootstrap/memories/${seed}"; then
      echo "seed memory must be visible to Git: ${profile}/${seed}" >&2
      exit 1
    fi
  done
  test "$(find "${base}/skills" -name SKILL.md -type f | wc -l | tr -d ' ')" -eq 1
  grep -qx '_config_version: 33' "${base}/config.yaml"
  grep -qx 'hermes_requires: ">=0.19.0"' "${base}/distribution.yaml"
  grep -qx 'version: 0.2.0' "${base}/distribution.yaml"
  grep -qx "HERMES_PROFILE=${profile}" "${base}/.env.template"
  grep -qx 'GITLAB_HOST=' "${base}/.env.template"
  grep -qx 'GITLAB_ALLOWED_GROUPS=' "${base}/.env.template"
  grep -qx 'GITLAB_TOKEN=' "${base}/.env.template"
done

for script in "${repo_root}"/scripts/*.sh; do
  test -x "${script}"
  bash -n "${script}"
done
for script in "${repo_root}"/scripts/*.py; do
  test -x "${script}"
  python3 - "${script}" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
compile(path.read_text(encoding="utf-8"), str(path), "exec")
PY
done

git apply --numstat "${repo_root}/patches/hermes-0.19.0-dispatcher-kanban-guard.patch" >/dev/null

python3 - "${repo_root}" <<'PY'
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse

root = pathlib.Path(sys.argv[1])
profiles = [
    "dispatcher", "prd-writer", "fde", "spec-writer", "spec-reviewer",
    "planner", "plan-reviewer", "tasker", "task-reviewer", "coder",
    "tester", "code-reviewer",
]
gateway_profiles = {"dispatcher", "prd-writer", "fde"}

for path in root.rglob("*.json"):
    json.loads(path.read_text(encoding="utf-8"))

lock = (root / "config/skills-lock.yaml").read_text(encoding="utf-8")
for value in [
    "image_mode: official-mounted",
    "tooling_volume_schema: 1",
    "release_tag: v2026.7.20",
    "revision: 3ef6bbd201263d354fd83ec55b3c306ded2eb72a",
    "docker_image: nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40",
    "amd64_manifest_digest: sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40",
    "minimum_version: 0.19.0",
    "config_version: 33",
]:
    assert value in lock, f"missing Hermes 0.19 lock: {value}"

assert not (root / "Dockerfile").exists(), "custom Dockerfile must not exist"
assert not (root / ".dockerignore").exists(), "build-only .dockerignore must not exist"

patch = (root / "patches/hermes-0.19.0-dispatcher-kanban-guard.patch").read_text(encoding="utf-8")
for value in [
    "HERMES_PROFILE", 'profile != "dispatcher"',
    '_require_dispatcher_profile("kanban_create")',
    '_require_dispatcher_profile("kanban_link")',
    "AND created_by = 'dispatcher'", "status = 'ready'", "status = 'review'",
]:
    assert value in patch, f"worker Kanban handler guard incomplete: {value}"

compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
assert 'command: ["sleep", "infinity"]' in compose
assert "build:" not in compose
locked_image = "nousresearch/hermes-agent:v2026.7.20@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40"
assert compose.count(f"image: ${{HERMES_IMAGE:-{locked_image}}}") == 2
assert compose.count("pull_policy: never") == 2
assert "tooling-sync:" in compose
assert "condition: service_completed_successfully" in compose
assert "tooling:/opt/fleet/vendor\n" in compose
assert "tooling:/opt/fleet/vendor:ro" in compose
assert "./profiles:/opt/fleet/profiles:ro" in compose
assert "./templates:/opt/fleet/templates:ro" in compose
assert "./schemas:/opt/fleet/schemas:ro" in compose
assert "./scripts:/opt/fleet/scripts:ro" in compose
assert "./patches:/opt/fleet/patches:ro" in compose
assert "./config:/opt/fleet/config:ro" in compose
assert "PATH: /command:/opt/fleet/vendor/current/bin:" in compose
for value in [
    "017-hermes-runtime-patch", "018-hermes-sdd-fleet",
    "021-hermes-sdd-gateways",
]:
    assert value in compose
assert compose.count("@sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40") == 2
for value in [
    'HERMES_DASHBOARD: "1"', 'HERMES_DASHBOARD_HOST: "0.0.0.0"',
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET", '"127.0.0.1:${HERMES_DASHBOARD_PORT:-9119}:9119"',
    "io.hermes.fleet.bundle-revision", "FLEET_BUNDLE_REF",
]:
    assert value in compose, f"authenticated Dashboard deployment contract missing: {value}"
assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD:" not in compose
assert "gateway run" not in compose
assert "env_file:" not in compose, "root .env must not be injected wholesale"
for forbidden in ["GITLAB_TOKEN", "GITLAB_HOST", "FEISHU_", "LARKSUITE_CLI_"]:
    assert forbidden not in compose, f"profile credential leaked into Compose: {forbidden}"

tooling_sync = (root / "scripts/sync-tooling.sh").read_text(encoding="utf-8")
for value in [
    "config/skills-lock.yaml", "releases/${release_name}", "sha256sum -c",
    "@larksuite/cli@${lark_cli_version}", "gitlab_skills_rev",
    "lark_skills_rev", "mv -Tf", ".tooling-lock",
]:
    assert value in tooling_sync, f"tooling volume sync contract missing: {value}"

runtime_patch = (root / "scripts/runtime-patch-hermes.sh").read_text(encoding="utf-8")
for value in [
    'version}" != "0.19.0"', "git -C \"${install_root}\" apply --check",
    "git -C \"${install_root}\" apply --reverse --check",
    "hermes-0.19.0-dispatcher-kanban-guard.patch", "dispatcher_required",
]:
    assert value in runtime_patch, f"runtime patch contract missing: {value}"

start_gateways = (root / "scripts/start-profile-gateways.sh").read_text(encoding="utf-8")
assert start_gateways.startswith("#!/command/with-contenv bash\n"), (
    "Gateway bootstrap must import the s6 container environment"
)
assert "for profile in dispatcher prd-writer fde" in start_gateways
assert "gateway start" in start_gateways
assert "gateway status" in start_gateways
assert "for attempt in 1 2 3 4 5" in start_gateways

bootstrap = (root / "scripts/container-bootstrap.sh").read_text(encoding="utf-8")
assert bootstrap.startswith("#!/command/with-contenv bash\n"), (
    "container bootstrap must import the s6 container environment"
)
for value in [
    "gateway_profiles=(dispatcher prd-writer fde)",
    "validate-profile-envs.py", "--profiles-root", "--owner hermes",
    ".lark-cli", "LARKSUITE_CLI_CONFIG_DIR", "LARKSUITE_CLI_DATA_DIR",
    "lark-cli config bind --source hermes --identity bot-only",
    "git config --global credential.helper '!glab auth git-credential'",
    "config set model.provider", "config set model.default",
    "FLEET_MODEL must use provider/model format",
]:
    assert value in bootstrap, f"bootstrap isolation contract missing: {value}"
for forbidden in [
    "write_profile_env", "validate_profile_env_file", "FLEET_SYNC_SECRETS",
    "lark-cli config init", "glab auth login", "GITLAB_TOKEN_DISPATCHER",
    "FEISHU_APP_ID_PRD_WRITER", "FEISHU_APP_ID_FDE", "GATEWAY_API_PORT_DISPATCHER",
]:
    assert forbidden not in bootstrap, f"bootstrap must not persist or overwrite credentials: {forbidden}"
identity_loop = re.search(r"for profile in ([^;]+); do\n\s+key=.*?GIT_COMMIT_NAME", bootstrap, re.S)
assert identity_loop and "fde" not in identity_loop.group(1), "FDE must not require Git identity"

runtime_verifier = (root / "scripts/verify-runtime.sh").read_text(encoding="utf-8")
for value in [
    'expected_provider, expected_model = fleet_model.split("/", 1)',
    'model.get("provider") != expected_provider',
    'model.get("default") != expected_model',
]:
    assert value in runtime_verifier, f"runtime model verification missing: {value}"

initializer = (root / "scripts/init-profile-envs.sh").read_text(encoding="utf-8")
for value in [
    "HERMES_DATA_DIR", ".env.template", "chmod 0600", "install -d -m 0700",
    "appeared concurrently; nothing was overwritten",
]:
    assert value in initializer, f"profile env initializer contract missing: {value}"

validator = (root / "scripts/validate-profile-envs.py").read_text(encoding="utf-8")
for value in [
    "must be unique:", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
    "not a symbolic link", "mode must be 0600", "profile directory mode must be 0700",
    "Feishu/Lark variables are not allowed", "hashlib.sha256",
    "--runtime-check", "validate_runtime_identities", "EXPECTED_ACCESS_LEVELS",
    "capture_output=True", "worker profile must not have lark-cli state",
    "GITLAB_HOST must match every other profile", "GITLAB_ALLOWED_GROUPS must match every other profile",
    "expected_feishu_policy", '"FEISHU_CONNECTION_MODE": "websocket"',
    '"FEISHU_ALLOW_ALL_USERS": "true"', '"FEISHU_GROUP_POLICY": "open"',
    '"FEISHU_REQUIRE_MENTION": "true"', '"FEISHU_ALLOW_BOTS": "mentions"',
    "access_level != required",
]:
    assert value in validator, f"profile env validator contract missing: {value}"

deployment_initializer = (root / "scripts/init-deployment-env.sh").read_text(encoding="utf-8")
for value in [
    "run this script interactively", "plugins.dashboard_auth.basic import hash_password",
    "openssl rand -base64 32", "FLEET_BUNDLE_REF", "chmod 0600",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH", "set +x",
]:
    assert value in deployment_initializer, f"deployment initializer contract missing: {value}"
assert "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=" not in deployment_initializer

deployment_validator = (root / "scripts/validate-deployment-env.py").read_text(encoding="utf-8")
for value in [
    "root .env mode must be 0600", "not a symbolic link", "FLEET_BUNDLE_REF",
    "scrypt", "dashboard signing secret", "plaintext dashboard password variable is forbidden",
    "FLEET_FORCE_CONFIG must be restored to 0", "locked Hermes 0.19.0 AMD64 digest",
]:
    assert value in deployment_validator, f"deployment env validator contract missing: {value}"

env_example = (root / ".env.example").read_text(encoding="utf-8")
for value in [
    f"HERMES_IMAGE={locked_image}", "FLEET_BUNDLE_REF=",
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=", "HERMES_DASHBOARD_BASIC_AUTH_SECRET=",
    "TOOLING_VOLUME_NAME=hermes-sdd-tooling",
    "FLEET_MODEL=", "FLEET_MODEL_BASE_URL=", "OPENAI_API_KEY=", "FLEET_FORCE_CONFIG=0",
    "GIT_COMMIT_EMAIL_PRD_WRITER=prd-writer-bot@hermes.invalid",
    "GIT_COMMIT_EMAIL_SPEC_WRITER=spec-writer-bot@hermes.invalid",
    "GIT_COMMIT_EMAIL_PLANNER=planner-bot@hermes.invalid",
    "GIT_COMMIT_EMAIL_TASKER=tasker-bot@hermes.invalid",
    "GIT_COMMIT_EMAIL_CODER=coder-bot@hermes.invalid",
]:
    assert value in env_example
assert "HERMES_BASE_IMAGE" not in env_example
assert "GIT_COMMIT_NAME_FDE" not in env_example
for forbidden in ["GITLAB_", "FEISHU_", "LARKSUITE_CLI_", "GATEWAY_API_PORT_", "FLEET_SYNC_SECRETS"]:
    assert forbidden not in env_example, f"root .env.example contains profile credential: {forbidden}"

compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
for value in ["FLEET_MODEL: ${FLEET_MODEL:-}", "FLEET_MODEL_BASE_URL: ${FLEET_MODEL_BASE_URL:-}"]:
    assert value in compose, f"Compose model environment contract missing: {value}"

bootstrap = (root / "scripts/container-bootstrap.sh").read_text(encoding="utf-8")
for value in ["config set model.provider", "config set model.default", "config set model.base_url"]:
    assert value in bootstrap, f"model bootstrap contract missing: {value}"

runtime_verifier = (root / "scripts/verify-runtime.sh").read_text(encoding="utf-8")
for value in ["FLEET_MODEL_BASE_URL", 'model.get("base_url")']:
    assert value in runtime_verifier, f"runtime model endpoint check missing: {value}"

disabled_skills = {
    "ascii-video", "comfyui", "manim-video", "touchdesigner-mcp",
    "hyperframes", "kanban-video-orchestrator", "blender-mcp", "gif-search",
    "youtube-content", "songwriting-and-ai-music", "heartmula", "songsee",
    "audiocraft-audio-generation", "openhue", "minecraft-modpack-server",
    "pokemon-player",
}

for profile in profiles:
    config = (root / f"profiles/{profile}/config.yaml").read_text(encoding="utf-8")
    env_template = (root / f"profiles/{profile}/.env.template").read_text(encoding="utf-8")
    disabled_match = re.search(r"(?m)^  disabled: \[([^]]*)\]$", config)
    assert disabled_match, f"missing skills.disabled: {profile}"
    actual_disabled = {
        item.strip() for item in disabled_match.group(1).split(",") if item.strip()
    }
    assert actual_disabled == disabled_skills, f"skills.disabled drift: {profile}"
    has_feishu = "platform_toolsets:\n  feishu:" in config
    assert has_feishu == (profile in gateway_profiles), f"unexpected Feishu gateway config: {profile}"
    assert "/opt/fleet/vendor/current/gitlab/skills" in config
    if profile in gateway_profiles:
        assert "/opt/fleet/vendor/current/lark/skills" in config
        assert "default_group_policy: open" in config, f"gateway group policy drift: {profile}"
        for value in [
            "API_SERVER_PORT=", "FEISHU_APP_ID=", "FEISHU_APP_SECRET=",
            "FEISHU_DOMAIN=feishu", "FEISHU_CONNECTION_MODE=websocket",
            "FEISHU_ALLOW_ALL_USERS=true", "FEISHU_ALLOWED_USERS=",
            "FEISHU_HOME_CHANNEL=", "FEISHU_GROUP_POLICY=open",
            "FEISHU_REQUIRE_MENTION=true", "FEISHU_ALLOW_BOTS=mentions",
            f"LARKSUITE_CLI_CONFIG_DIR=/opt/data/profiles/{profile}/.lark-cli/config",
            f"LARKSUITE_CLI_DATA_DIR=/opt/data/profiles/{profile}/.lark-cli/data",
        ]:
            assert value in env_template, f"gateway env template incomplete: {profile}: {value}"
    else:
        for forbidden in ["API_SERVER_PORT", "FEISHU_", "LARKSUITE_CLI_"]:
            assert forbidden not in env_template, f"worker has Feishu/Lark configuration: {profile}"
    assert "OPENAI_API_KEY=" not in env_template, "model credentials remain deployment-level"
    assert "home_mode: profile" in config, f"profile home isolation missing: {profile}"
    assert "memory:\n" in config and "  write_approval: true" in config, f"memory approval missing: {profile}"
    if profile == "dispatcher":
        assert "dispatch_in_gateway: true" in config
        assert "auto_decompose: false" in config
    elif profile in {"prd-writer", "fde"}:
        assert "dispatch_in_gateway: false" in config
    else:
        assert "dispatch_in_gateway: true" not in config

schema = json.loads((root / "schemas/card-completion.schema.json").read_text(encoding="utf-8"))
assert schema["properties"]["schema_version"]["const"] == 2
required = set(schema["required"])
for value in [
    "project_id", "project_path", "project_display_name", "checkout", "worktree",
    "branch", "target_branch", "prd_path", "prd_commit_sha", "prd_mr_url",
    "kanban_card_id", "mr_iid", "mr_url", "head_sha", "artifact_paths",
    "artifact_digest",
]:
    assert value in required, f"schema v2 missing required field: {value}"
stages = set(schema["properties"]["stage"]["enum"])
for value in [
    "spec-write", "spec-review", "plan-write", "plan-review", "tasks-write",
    "tasks-review", "implement", "test", "code-review", "merge", "run-complete",
]:
    assert value in stages
assert "next-spec-or-complete" not in stages
conditions = schema.get("allOf", [])
assert len(conditions) >= 3, "conditional completion gates are missing"
schema_text = json.dumps(schema, sort_keys=True)
for value in ["checked_head", "merge_commit_sha", "spec-review", "code-review", "sha=checked_head"]:
    assert value in schema_text, f"conditional completion schema missing: {value}"

card = (root / "templates/kanban-card.md").read_text(encoding="utf-8")
for value in [
    "schema_version: 2", "created_by: dispatcher", "project_display_name:",
    "checkout:", "worktree:", "prd_mr_url:", "artifact_digest:",
    "never create a second delivery branch or MR", "card_role:", "transition_key:",
    "awaits_parent_card_id:", "live_reconcile_required: true", ":work", ":continue",
]:
    assert value in card, f"card v2 contract missing: {value}"

gate = (root / "templates/gate-comment.md").read_text(encoding="utf-8")
for value in ["SDD-GATE: v=2", "artifact_digest", "review_commit_sha", "head_sha"]:
    assert value in gate

mr = (root / "templates/mr-description.md").read_text(encoding="utf-8")
for value in ["schema_version: 2", "source_prd:", "specs:", "plans:", "tasks:", "merge_commit_sha"]:
    assert value in mr
assert "artifact_type:" not in mr

feishu = (root / "templates/feishu-messages.md").read_text(encoding="utf-8")
assert "实现 PRD <精确 PRD blob/raw URL> <已合并 PRD MR URL>" in feishu
for value in ["原 `chat_id`", "thread_id", "@initiator_open_id", "文件不存在", "已归档"]:
    assert value in feishu

assert not (root / "WORKFLOW.md").exists(), "README must be the only workflow authority"
readme = (root / "README.md").read_text(encoding="utf-8")
for value in [
    "## 1. 总体介绍", "## 2. 流程设计", "## 3. 部署说明",
    "#### 1.3.1 Hermes 内置 Skill 边界", "skills.disabled",
    "一个共享分支", "一个 Draft MR", "artifact_digest", "sha=<checked_head>",
    "SPEC、PLAN、TASKS 各阶段最多返工 3 轮",
    "代码、测试、代码审查引起的代码返工合计最多 5 轮",
    "./scripts/verify-runtime.sh", "生产自治 E2E", "双卡协议",
    "./scripts/init-deployment-env.sh", "auth_required=true", "内网受控试运行",
]:
    assert value in readme, f"authoritative README workflow/deployment content missing: {value}"

assert not (root / "scripts/workflow_contract.py").exists(), "test-only helper leaked into runtime scripts"
assert (root / "tests/workflow_contract.py").is_file(), "test-only workflow helper is missing"

runtime_verifier = (root / "scripts/verify-runtime.sh").read_text(encoding="utf-8")
for value in [
    "sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40",
    "expected Hermes 0.19.0",
    "gateway_profiles=(dispatcher prd-writer fde)", "s6-svstat -o up",
    "dispatcher-only Kanban guard", ".tooling-lock", "--runtime-check",
    "deployable read-only runtime checks passed", "Dashboard /api/status",
    "auth_required", "auth_providers", "memory.write_approval must be true",
    "validate-deployment-env.py", "git fsck --full",
]:
    assert value in runtime_verifier, f"runtime verifier contract missing: {value}"
for forbidden in ["set -x", "source /opt/data/profiles", "glab auth login"]:
    assert forbidden not in runtime_verifier, f"runtime verifier may leak or persist credentials: {forbidden}"

seed_memory = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((root / "profiles").glob("*/bootstrap/memories/*.md"))
)
assert "一个 PRD 合入版本" in seed_memory
for stale in ["每个 SPEC 一个代码 MR", "一个 SPEC 对应一个 code MR", "SPEC gate 必须绑定当前 MR head"]:
    assert stale not in seed_memory, f"stale seed memory contract remains: {stale}"

skills = "\n".join(
    path.read_text(encoding="utf-8")
    for path in sorted((root / "profiles").glob("*/skills/*/SKILL.md"))
)
for value in [
    "docs/prds/<prd-basename>/specs/spec-<key>.md",
    "docs/prds/<prd-basename>/plans/plan-<key>.md",
    "docs/prds/<prd-basename>/tasks/task-<key>.md",
    "Draft: [PRD] <prd-basename>.md",
    "created_by=dispatcher",
    "Worker/continuation pair protocol", ":work", ":continue",
]:
    assert value in skills, f"profile Skills missing single-MR contract: {value}"
for stale in [
    "specs/<feature-key>/spec.md", "artifact_type:",
    "one approved SPEC task set in one code merge request",
    "当前 SPEC 的代码 MR合入后才推进下一 SPEC",
]:
    assert stale not in skills

for profile in profiles:
    if profile == "dispatcher":
        continue
    skill = next((root / f"profiles/{profile}/skills").glob("*/SKILL.md")).read_text(encoding="utf-8")
    assert "kanban_create" not in skill and "kanban_link" not in skill, f"worker shapes Kanban DAG: {profile}"

missing = []
pattern = re.compile(r"\[[^]]*\]\(([^)]+)\)")
tracked_markdown = subprocess.run(
    ["git", "-C", str(root), "ls-files", "-z", "--", "*.md"],
    check=True,
    capture_output=True,
).stdout.decode("utf-8").split("\0")
for relative in filter(None, tracked_markdown):
    source = root / relative
    for raw_target in pattern.findall(source.read_text(encoding="utf-8")):
        target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        path_part = urllib.parse.unquote(target.split("#", 1)[0])
        if path_part and not (source.parent / path_part).resolve().exists():
            missing.append(f"{source.relative_to(root)} -> {target}")
if missing:
    raise SystemExit("missing local Markdown targets:\n" + "\n".join(missing))

print("bundle, Hermes 0.19 lock, profile credentials, gateways, schema v2 and single-MR contracts: ok")
PY

tracked_temporary=$(git -C "${repo_root}" ls-files | \
  awk -F/ '$NF == ".DS_Store" || $0 ~ /(^|\/)__pycache__(\/|$)/ || $NF ~ /\.pyc$/')
if [[ -n "${tracked_temporary}" ]]; then
  echo "tracked temporary files must be removed:" >&2
  echo "${tracked_temporary}" >&2
  exit 1
fi

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
  echo "YAML parser unavailable; install a parser before runtime deployment" >&2
fi

python3 -m unittest discover -s "${repo_root}/tests" -v

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin \
  HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH='scrypt$16384$8$1$c3Nzc3Nzc3Nzc3Nzc3Nzcw==$ZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGRkZGQ=' \
  HERMES_DASHBOARD_BASIC_AUTH_SECRET='a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=' \
  FLEET_BUNDLE_REF=1111111111111111111111111111111111111111 \
    docker compose --env-file "${repo_root}/.env.example" \
    -f "${repo_root}/docker-compose.yml" config --quiet
  echo "Docker Compose config: ok"
fi
