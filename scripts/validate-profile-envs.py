#!/usr/bin/env python3
"""Validate profile-owned GitLab and Feishu credentials without sourcing them."""

from __future__ import annotations

import argparse
import ast
import hashlib
import pathlib
import pwd
import re
import stat
import sys
from collections.abc import Iterable


PROFILES = (
    "dispatcher",
    "prd-writer",
    "fde",
    "spec-writer",
    "spec-reviewer",
    "planner",
    "plan-reviewer",
    "tasker",
    "task-reviewer",
    "coder",
    "tester",
    "code-reviewer",
)
GATEWAY_PROFILES = ("dispatcher", "prd-writer", "fde")
COMMON_REQUIRED = (
    "HERMES_PROFILE",
    "GITLAB_HOST",
    "GITLAB_ALLOWED_GROUPS",
    "GITLAB_TOKEN",
)
GATEWAY_REQUIRED = (
    "API_SERVER_PORT",
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_DOMAIN",
    "FEISHU_CONNECTION_MODE",
    "FEISHU_ALLOWED_USERS",
    "FEISHU_HOME_CHANNEL",
    "FEISHU_GROUP_POLICY",
    "FEISHU_REQUIRE_MENTION",
    "LARKSUITE_CLI_CONFIG_DIR",
    "LARKSUITE_CLI_DATA_DIR",
)
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProfileEnvError(ValueError):
    pass


def _parse_value(raw: str, *, path: pathlib.Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        end = None
        for index, char in enumerate(value[1:], start=1):
            if char == quote and not escaped:
                end = index
                break
            if char == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
        if end is None:
            raise ProfileEnvError(f"{path}: line {line_number}: unterminated quoted value")
        suffix = value[end + 1 :].strip()
        if suffix and not suffix.startswith("#"):
            raise ProfileEnvError(f"{path}: line {line_number}: unexpected text after quoted value")
        try:
            parsed = ast.literal_eval(value[: end + 1])
        except (SyntaxError, ValueError) as exc:
            raise ProfileEnvError(f"{path}: line {line_number}: invalid quoted value") from exc
        if not isinstance(parsed, str):
            raise ProfileEnvError(f"{path}: line {line_number}: value must be text")
        return parsed

    for index, char in enumerate(value):
        if char == "#" and index > 0 and value[index - 1].isspace():
            value = value[:index].rstrip()
            break
    return value.rstrip()


def parse_dotenv(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileEnvError(f"{path}: must be UTF-8 text") from exc
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ProfileEnvError(f"{path}: line {line_number}: expected KEY=value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise ProfileEnvError(f"{path}: line {line_number}: invalid variable name")
        if key in values:
            raise ProfileEnvError(f"{path}: line {line_number}: duplicate variable {key}")
        values[key] = _parse_value(raw_value, path=path, line_number=line_number)
    return values


def _secret_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _check_unique(
    profile_values: dict[str, dict[str, str]], key: str, profiles: Iterable[str], errors: list[str]
) -> None:
    seen: dict[str, str] = {}
    for profile in profiles:
        value = profile_values.get(profile, {}).get(key, "")
        if not value:
            continue
        fingerprint = _secret_fingerprint(value)
        if fingerprint in seen:
            errors.append(f"{key} must be unique: {seen[fingerprint]} and {profile} match")
        else:
            seen[fingerprint] = profile


def validate_profiles(profiles_root: pathlib.Path, expected_uid: int) -> dict[str, dict[str, str]]:
    errors: list[str] = []
    profile_values: dict[str, dict[str, str]] = {}
    ports: dict[str, str] = {}

    for profile in PROFILES:
        profile_dir = profiles_root / profile
        env_path = profile_dir / ".env"
        try:
            dir_status = profile_dir.lstat()
            if stat.S_ISLNK(dir_status.st_mode) or not stat.S_ISDIR(dir_status.st_mode):
                errors.append(f"{profile}: profile path must be a real directory")
                continue
            if stat.S_IMODE(dir_status.st_mode) != 0o700:
                errors.append(f"{profile}: profile directory mode must be 0700")
        except FileNotFoundError:
            errors.append(f"{profile}: profile directory is missing; run scripts/init-profile-envs.sh")
            continue

        try:
            file_status = env_path.lstat()
        except FileNotFoundError:
            errors.append(f"{profile}: .env is missing; run scripts/init-profile-envs.sh")
            continue
        if stat.S_ISLNK(file_status.st_mode) or not stat.S_ISREG(file_status.st_mode):
            errors.append(f"{profile}: .env must be a regular file, not a symbolic link")
            continue
        if stat.S_IMODE(file_status.st_mode) != 0o600:
            errors.append(f"{profile}: .env mode must be 0600")
        if file_status.st_uid != expected_uid:
            errors.append(f"{profile}: .env must be owned by the Hermes runtime user")

        try:
            values = parse_dotenv(env_path)
        except (OSError, ProfileEnvError) as exc:
            errors.append(str(exc))
            continue
        profile_values[profile] = values

        for key in COMMON_REQUIRED:
            if not values.get(key):
                errors.append(f"{profile}: required variable {key} is empty")
        if values.get("HERMES_PROFILE") != profile:
            errors.append(f"{profile}: HERMES_PROFILE must equal {profile}")
        if "example" in values.get("GITLAB_HOST", "").lower():
            errors.append(f"{profile}: GITLAB_HOST still contains an example placeholder")

        if profile in GATEWAY_PROFILES:
            for key in GATEWAY_REQUIRED:
                if not values.get(key):
                    errors.append(f"{profile}: required variable {key} is empty")
            port = values.get("API_SERVER_PORT", "")
            if not port.isdigit() or not 10 <= int(port or 0) <= 65535:
                errors.append(f"{profile}: API_SERVER_PORT must be between 10 and 65535")
            elif port in ports:
                errors.append(f"API_SERVER_PORT must be unique: {ports[port]} and {profile} match")
            else:
                ports[port] = profile
            for key in ("FEISHU_ALLOWED_USERS", "FEISHU_HOME_CHANNEL"):
                if "replace_me" in values.get(key, ""):
                    errors.append(f"{profile}: {key} still contains a placeholder")
            expected_config = f"/opt/data/profiles/{profile}/.lark-cli/config"
            expected_data = f"/opt/data/profiles/{profile}/.lark-cli/data"
            if values.get("LARKSUITE_CLI_CONFIG_DIR") != expected_config:
                errors.append(f"{profile}: LARKSUITE_CLI_CONFIG_DIR must equal {expected_config}")
            if values.get("LARKSUITE_CLI_DATA_DIR") != expected_data:
                errors.append(f"{profile}: LARKSUITE_CLI_DATA_DIR must equal {expected_data}")
        else:
            forbidden = sorted(
                key
                for key in values
                if key == "API_SERVER_PORT"
                or key.startswith("FEISHU_")
                or key.startswith("LARKSUITE_CLI_")
            )
            if forbidden:
                errors.append(f"{profile}: Feishu/Lark variables are not allowed: {', '.join(forbidden)}")

    _check_unique(profile_values, "GITLAB_TOKEN", PROFILES, errors)
    _check_unique(profile_values, "FEISHU_APP_ID", GATEWAY_PROFILES, errors)
    _check_unique(profile_values, "FEISHU_APP_SECRET", GATEWAY_PROFILES, errors)

    if errors:
        raise ProfileEnvError("\n".join(f"profile env validation: {error}" for error in errors))
    return profile_values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles-root", type=pathlib.Path, required=True)
    parser.add_argument("--owner", default="hermes")
    args = parser.parse_args()
    try:
        expected_uid = pwd.getpwnam(args.owner).pw_uid
    except KeyError:
        print(f"profile env validation: runtime user {args.owner!r} does not exist", file=sys.stderr)
        return 65
    try:
        validate_profiles(args.profiles_root, expected_uid)
    except ProfileEnvError as exc:
        print(exc, file=sys.stderr)
        return 65
    print("profile env validation: 12 isolated GitLab identities and 3 Feishu identities are configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
