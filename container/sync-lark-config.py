#!/usr/bin/env python3
"""Synchronize lark-cli bot identities from the authoritative Profile envs."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path

GATEWAY_PROFILES = ("dispatcher", "fde", "prd-writer")
PLACEHOLDERS = {
    "REPLACE_WITH_FEISHU_APP_ID",
    "REPLACE_WITH_FEISHU_APP_SECRET",
}


def dotenv_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        parts = shlex.split(raw_value.strip(), comments=False, posix=True)
        values[key.strip()] = parts[0] if len(parts) == 1 else raw_value.strip()
    return values


def assert_no_symlink(path: Path, stop: Path) -> None:
    current = path
    while current != stop:
        if current.is_symlink():
            raise RuntimeError(f"lark config path is a symlink: {current}")
        current = current.parent


def synchronize_profile(profiles_root: Path, profile: str) -> None:
    profile_root = profiles_root / profile
    env_path = profile_root / ".env"
    if env_path.is_symlink() or not env_path.is_file():
        raise RuntimeError(f"missing regular Profile env: {profile}")
    values = dotenv_values(env_path)
    app_id = values.get("FEISHU_APP_ID", "")
    app_secret = values.get("FEISHU_APP_SECRET", "")
    if (
        not app_id
        or not app_secret
        or app_id in PLACEHOLDERS
        or app_secret in PLACEHOLDERS
    ):
        raise RuntimeError(f"missing Feishu bot credentials: {profile}")

    target = (
        profile_root
        / ".lark-cli"
        / "config"
        / "hermes"
        / "config.json"
    )
    assert_no_symlink(target, profiles_root)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.parent.chmod(0o700)
    if target.is_symlink():
        raise RuntimeError(f"lark config target is a symlink: {profile}")

    payload = {
        "apps": [
            {
                "name": profile,
                "appId": app_id,
                "appSecret": app_secret,
                "brand": "feishu",
                "defaultAs": "bot",
                "strictMode": "bot",
                "users": [],
            }
        ]
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        target.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> None:
    profiles_root = Path(
        os.environ.get("HOLLYSYS_PROFILES_ROOT", "/opt/data/profiles")
    )
    if profiles_root.is_symlink() or not profiles_root.is_dir():
        raise RuntimeError("Profile root must be a regular directory")
    for profile in GATEWAY_PROFILES:
        synchronize_profile(profiles_root, profile)
    print(
        "sync-lark-config: synchronized "
        f"{len(GATEWAY_PROFILES)} gateway profiles"
    )


if __name__ == "__main__":
    main()
