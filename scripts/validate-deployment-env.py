#!/usr/bin/env python3
"""Validate the root deployment environment without sourcing or printing it."""

from __future__ import annotations

import argparse
import ast
import base64
import binascii
import os
import pathlib
import re
import stat
import subprocess
import sys


EXPECTED_IMAGE = (
    "nousresearch/hermes-agent:v2026.7.20@"
    "sha256:a6ce64e2038867885c2c90f6602425e6e70293d5e6d952a0e603a99265e01c40"
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class DeploymentEnvError(ValueError):
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
            raise DeploymentEnvError(f"{path}: line {line_number}: unterminated quoted value")
        suffix = value[end + 1 :].strip()
        if suffix and not suffix.startswith("#"):
            raise DeploymentEnvError(f"{path}: line {line_number}: unexpected text after quoted value")
        try:
            parsed = ast.literal_eval(value[: end + 1])
        except (SyntaxError, ValueError) as exc:
            raise DeploymentEnvError(f"{path}: line {line_number}: invalid quoted value") from exc
        if not isinstance(parsed, str):
            raise DeploymentEnvError(f"{path}: line {line_number}: value must be text")
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
        raise DeploymentEnvError(f"{path}: must be UTF-8 text") from exc
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise DeploymentEnvError(f"{path}: line {line_number}: expected KEY=value")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise DeploymentEnvError(f"{path}: line {line_number}: invalid variable name")
        if key in values:
            raise DeploymentEnvError(f"{path}: line {line_number}: duplicate variable {key}")
        values[key] = _parse_value(raw_value, path=path, line_number=line_number)
    return values


def _decode_base64(value: str, *, field: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise DeploymentEnvError(f"{field} must be valid base64") from exc


def validate_deployment_env(
    env_path: pathlib.Path,
    *,
    expected_uid: int,
    allow_force_config: bool = False,
) -> dict[str, str]:
    try:
        status = env_path.lstat()
    except FileNotFoundError as exc:
        raise DeploymentEnvError("root .env is missing; run scripts/init-deployment-env.sh") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise DeploymentEnvError("root .env must be a regular file, not a symbolic link")
    if stat.S_IMODE(status.st_mode) != 0o600:
        raise DeploymentEnvError("root .env mode must be 0600")
    if status.st_uid != expected_uid:
        raise DeploymentEnvError("root .env must be owned by the deployment user")

    values = parse_dotenv(env_path)
    raw_text = env_path.read_text(encoding="utf-8")
    required = (
        "HERMES_IMAGE",
        "FLEET_BUNDLE_REF",
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
        "FLEET_FORCE_CONFIG",
    )
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise DeploymentEnvError(f"root .env required variables are empty: {', '.join(missing)}")
    if "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD" in values:
        raise DeploymentEnvError("plaintext dashboard password variable is forbidden in root .env")
    quoted_hash = re.search(
        r"(?m)^\s*(?:export\s+)?HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH='[^']+'\s*(?:#.*)?$",
        raw_text,
    )
    if not quoted_hash:
        raise DeploymentEnvError("dashboard password hash must be single-quoted in root .env")
    if values["HERMES_IMAGE"] != EXPECTED_IMAGE:
        raise DeploymentEnvError("HERMES_IMAGE must equal the locked Hermes 0.19.0 AMD64 digest")
    if not SHA_RE.fullmatch(values["FLEET_BUNDLE_REF"]):
        raise DeploymentEnvError("FLEET_BUNDLE_REF must be one exact 40-character commit SHA")
    if values["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] != "admin":
        raise DeploymentEnvError("dashboard username must equal admin")

    encoded_hash = values["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH"]
    parts = encoded_hash.split("$")
    if len(parts) != 6 or parts[:4] != ["scrypt", "16384", "8", "1"]:
        raise DeploymentEnvError("dashboard password hash must use Hermes scrypt parameters")
    if len(_decode_base64(parts[4], field="dashboard password salt")) != 16:
        raise DeploymentEnvError("dashboard password hash salt must be 16 bytes")
    if len(_decode_base64(parts[5], field="dashboard password digest")) != 32:
        raise DeploymentEnvError("dashboard password hash digest must be 32 bytes")

    signing_secret = _decode_base64(
        values["HERMES_DASHBOARD_BASIC_AUTH_SECRET"],
        field="dashboard signing secret",
    )
    if len(signing_secret) != 32:
        raise DeploymentEnvError("dashboard signing secret must decode to 32 random bytes")
    if values["FLEET_FORCE_CONFIG"] not in {"0", "1"}:
        raise DeploymentEnvError("FLEET_FORCE_CONFIG must be 0 or 1")
    if values["FLEET_FORCE_CONFIG"] != "0" and not allow_force_config:
        raise DeploymentEnvError("FLEET_FORCE_CONFIG must be restored to 0 before runtime acceptance")
    return values


def validate_repository_lock(repo_root: pathlib.Path, expected_revision: str) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentEnvError("could not read the deployment repository HEAD") from exc
    if result.returncode != 0 or result.stdout.strip() != expected_revision:
        raise DeploymentEnvError("deployed checkout HEAD does not match FLEET_BUNDLE_REF")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=pathlib.Path, required=True)
    parser.add_argument("--allow-force-config", action="store_true")
    parser.add_argument("--print-bundle-ref", action="store_true")
    args = parser.parse_args()
    try:
        values = validate_deployment_env(
            args.env_file,
            expected_uid=os.getuid(),
            allow_force_config=args.allow_force_config,
        )
    except (OSError, DeploymentEnvError) as exc:
        print(f"deployment env validation: {exc}", file=sys.stderr)
        return 65
    if args.print_bundle_ref:
        print(values["FLEET_BUNDLE_REF"])
    else:
        print("deployment env validation: image digest and hashed dashboard auth are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
