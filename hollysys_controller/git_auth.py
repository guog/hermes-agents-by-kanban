from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from .config import ControllerConfig, trusted_runtime_uids

WRITER_PROFILES = frozenset(
    {"prd-writer", "spec-writer", "planner", "tasker", "coder"}
)
READ_ONLY_PROFILES = frozenset(
    {
        "dispatcher",
        "fde",
        "spec-reviewer",
        "plan-reviewer",
        "task-reviewer",
        "tester",
        "code-reviewer",
    }
)
ALL_PROFILES = WRITER_PROFILES | READ_ONLY_PROFILES


@dataclass(frozen=True)
class ProfileCredential:
    profile: str
    host_url: str
    allowed_groups: tuple[str, ...]
    token: str
    env_path: Path
    home: Path


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


def read_profile_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if key:
            values[key] = _dotenv_value(raw_value)
    return values


def profile_credential(config: ControllerConfig, profile: str) -> ProfileCredential:
    if profile not in ALL_PROFILES:
        raise ValueError(f"unknown_profile:{profile}")
    env_path = config.profiles_root / profile / ".env"
    if not env_path.exists():
        raise ValueError(f"missing_profile_identity:{profile}")
    if env_path.is_symlink():
        raise PermissionError(f"profile_env_symlink:{profile}")
    info = env_path.stat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid not in trusted_runtime_uids()
    ):
        raise PermissionError(f"profile_env_permissions:{profile}")
    values = read_profile_env(env_path)
    if not values.get("HERMES_PROFILE"):
        raise ValueError(f"missing_profile_identity:{profile}")
    if not values.get("GITLAB_TOKEN"):
        raise ValueError(f"missing_gitlab_token:{profile}")
    if not values.get("GITLAB_HOST"):
        raise ValueError(f"invalid_gitlab_host:{profile}")
    if not values.get("GITLAB_ALLOWED_GROUPS"):
        raise ValueError(f"profile_allowed_groups_missing:{profile}")
    if values["HERMES_PROFILE"] != profile:
        raise ValueError(f"profile_identity_mismatch:{profile}")
    if values["GITLAB_HOST"] != config.gitlab_base_url:
        raise ValueError(f"invalid_gitlab_host:{profile}")
    endpoint = config.normalize_gitlab_endpoint(values["GITLAB_HOST"])
    if endpoint.hostname != config.gitlab_hostname:
        raise ValueError(f"remote_host_mismatch:{profile}")
    groups = tuple(
        part.strip()
        for part in values["GITLAB_ALLOWED_GROUPS"].split(",")
        if part.strip()
    )
    if not groups:
        raise ValueError(f"profile_allowed_groups_missing:{profile}")
    controller_groups = set(config.allowed_groups)
    if any(group not in controller_groups for group in groups):
        raise ValueError(f"profile_allowed_groups_mismatch:{profile}")
    return ProfileCredential(
        profile=profile,
        host_url=endpoint.base_url,
        allowed_groups=groups,
        token=values["GITLAB_TOKEN"],
        env_path=env_path,
        home=config.profiles_root / profile / "home",
    )


def _safe_env(credential: ProfileCredential) -> dict[str, str]:
    inherited = (
        "PATH",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "no_proxy",
    )
    env = {
        key: value
        for key in inherited
        if (value := os.environ.get(key)) is not None
    }
    env.update(
        {
            "HOME": str(credential.home),
            "HERMES_PROFILE": credential.profile,
            "GITLAB_HOST": credential.host_url,
            "GITLAB_ALLOWED_GROUPS": ",".join(credential.allowed_groups),
            "GITLAB_TOKEN": credential.token,
            "GLAB_CHECK_UPDATE": "false",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            input=input_text,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            command,
            124,
            stdout="",
            stderr="command_timeout",
        )
    except OSError:
        return subprocess.CompletedProcess(
            command,
            127,
            stdout="",
            stderr="command_unavailable",
        )


def _failure_code(
    result: subprocess.CompletedProcess[str],
    default: str,
) -> str:
    if result.returncode == 124:
        return "dependency_unavailable"
    if result.returncode == 127:
        return "command_unavailable"
    text = f"{result.stdout}\n{result.stderr}".lower()
    wrapper_codes = (
        "missing_profile_identity",
        "missing_gitlab_token",
        "invalid_gitlab_host",
        "ssh_disabled",
        "project_access_denied",
        "repository_read_forbidden",
        "repository_write_forbidden",
        "push_forbidden_for_role",
        "remote_host_mismatch",
        "git_environment_override_forbidden",
    )
    for code in wrapper_codes:
        if f"error={code}" in text:
            return code
    if "401" in text or "unauthorized" in text:
        return "token_expired_or_revoked"
    if "403" in text or "forbidden" in text:
        if default in {
            "repository_read_forbidden",
            "repository_write_forbidden",
        }:
            return default
        return "project_access_denied"
    if "authentication failed" in text or "access denied" in text:
        return "token_rejected"
    return default


def _decoded_object(result: subprocess.CompletedProcess[str]) -> dict | None:
    if result.returncode != 0:
        return None
    try:
        decoded = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _profile_token_is_persisted(credential: ProfileCredential) -> bool:
    candidates = (
        credential.home / ".config" / "glab-cli" / "config.yml",
        credential.home / ".gitconfig",
    )
    for path in candidates:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if (
            credential.token in text
            or bool(re.search(r"(?m)^\s*token:\s*\S+", text))
            or bool(re.search(r"https://[^/\s:@]+:[^/\s@]+@", text))
        ):
            return True
    return False


def profile_preflight(
    config: ControllerConfig,
    profile: str,
    *,
    deep: bool,
) -> dict:
    try:
        credential = profile_credential(config, profile)
    except (OSError, ValueError) as exc:
        return {
            "profile": profile,
            "ok": False,
            "error_code": str(exc).split(":", 1)[0],
        }
    git_config_path = credential.home / ".gitconfig"
    try:
        git_config_digest = hashlib.sha256(
            git_config_path.read_bytes()
        ).hexdigest()
    except OSError:
        git_config_digest = "missing"
    result: dict = {
        "profile": profile,
        "role": "writer" if profile in WRITER_PROFILES else "read-only",
        "config_ok": True,
        "api_ok": None,
        "repository_read_ok": None,
        "repository_write_ok": None,
        "https_username_ok": True,
        "remote_protocol": "https",
        "token_fingerprint": hashlib.sha256(
            credential.token.encode("utf-8")
        ).hexdigest(),
        "_credential_scope": (
            f"{credential.host_url}|"
            f"{','.join(credential.allowed_groups)}|"
            f"{'writer' if profile in WRITER_PROFILES else 'read-only'}|"
            f"gitconfig={git_config_digest}"
        ),
        "ok": True,
    }
    try:
        result["glab_token_persisted"] = _profile_token_is_persisted(
            credential
        )
    except OSError:
        result.update({"ok": False, "error_code": "profile_config_unreadable"})
        return result
    if result["glab_token_persisted"]:
        result.update({"ok": False, "error_code": "token_persisted"})
        return result
    if not deep:
        return result
    if not config.preflight_project_path:
        result.update(
            {
                "ok": False,
                "error_code": "preflight_project_missing",
            }
        )
        return result
    env = _safe_env(credential)
    login_bin = Path(config.agent_git_command).parent
    login_shell_contract = (
        (
            "git",
            config.agent_git_command,
            "git_path_ok",
            "git_wrapper_not_on_path",
        ),
        (
            "glab",
            str(login_bin / "glab"),
            "glab_path_ok",
            "glab_not_on_path",
        ),
        (
            "lark-cli",
            str(login_bin / "lark-cli"),
            "lark_cli_path_ok",
            "lark_cli_not_on_path",
        ),
    )
    for command, expected_path, result_key, error_code in login_shell_contract:
        terminal_command = _run(
            ["/bin/sh", "-lc", f"command -v {command}"],
            env=env,
            timeout=config.preflight_command_timeout_seconds,
        )
        result[result_key] = (
            terminal_command.returncode == 0
            and terminal_command.stdout.strip() == expected_path
        )
        if not result[result_key]:
            result.update({"ok": False, "error_code": error_code})
            return result
    api = _run(
        [
            config.glab_command,
            "api",
            "user",
            "--hostname",
            config.gitlab_hostname,
        ],
        env=env,
        timeout=config.preflight_command_timeout_seconds,
    )
    result["api_ok"] = api.returncode == 0
    user = _decoded_object(api)
    if user is None or not user.get("id"):
        result.update(
            {
                "ok": False,
                "error_code": _failure_code(api, "token_rejected"),
            }
        )
        return result
    result["identity_id"] = user["id"]

    project = quote(config.preflight_project_path, safe="")
    membership = _run(
        [
            config.glab_command,
            "api",
            f"projects/{project}/members/all/{user['id']}",
            "--hostname",
            config.gitlab_hostname,
        ],
        env=env,
        timeout=config.preflight_command_timeout_seconds,
    )
    member = _decoded_object(membership)
    if member is None:
        result.update(
            {
                "ok": False,
                "error_code": _failure_code(
                    membership,
                    "project_access_denied",
                ),
            }
        )
        return result
    access_level = int(member.get("access_level") or 0)
    required_access = 30 if profile in WRITER_PROFILES else 20
    if access_level < required_access:
        result.update({"ok": False, "error_code": "project_access_denied"})
        return result

    repository_url = (
        f"{config.gitlab_base_url}/{config.preflight_project_path}.git"
    )
    repository_read = _run(
        [config.agent_git_command, "ls-remote", repository_url, "HEAD"],
        env=env,
        timeout=config.preflight_command_timeout_seconds,
    )
    result["repository_read_ok"] = repository_read.returncode == 0
    if repository_read.returncode != 0:
        result.update(
            {
                "ok": False,
                "error_code": _failure_code(
                    repository_read,
                    "repository_read_forbidden",
                ),
            }
        )
        return result

    # Feed the credential query on stdin; neither the token nor password is
    # placed in argv.
    credential_probe = _run(
        [config.system_git_command, "credential", "fill"],
        input_text=(
            "protocol=https\n"
            f"host={config.gitlab_hostname}\n"
            f"path={config.preflight_project_path}.git\n\n"
        ),
        env=env,
        timeout=config.preflight_command_timeout_seconds,
    )
    credential_values = {
        key: value
        for line in credential_probe.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }
    result["https_username_ok"] = (
        credential_probe.returncode == 0
        and credential_values.get("username") == "oauth2"
        and credential_values.get("password") == credential.token
    )
    if not result["https_username_ok"]:
        result.update({"ok": False, "error_code": "empty_https_username"})
        return result

    wrapper = Path(config.agent_git_command)
    if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
        result.update({"ok": False, "error_code": "git_wrapper_missing"})
        return result
    with tempfile.TemporaryDirectory(
        prefix=f"hollysys-preflight-{profile}-",
        dir=config.state_dir,
    ) as temporary:
        checkout = Path(temporary) / "repository"
        cloned = _run(
            [config.agent_git_command, "clone", repository_url, str(checkout)],
            env=env,
            timeout=max(30, config.preflight_command_timeout_seconds),
        )
        if cloned.returncode != 0:
            result.update(
                {
                    "ok": False,
                    "error_code": _failure_code(
                        cloned,
                        "repository_read_forbidden",
                    ),
                }
            )
            return result
        origin = _run(
            [config.system_git_command, "-C", str(checkout), "remote", "get-url", "origin"],
            env=env,
            timeout=config.preflight_command_timeout_seconds,
        )
        origin_url = origin.stdout.strip()
        parsed_origin = urlparse(origin_url)
        result["origin_token_free"] = (
            origin.returncode == 0
            and parsed_origin.scheme == "https"
            and parsed_origin.hostname == config.gitlab_hostname
            and parsed_origin.username is None
            and parsed_origin.password is None
        )
        if not result["origin_token_free"]:
            result.update({"ok": False, "error_code": "remote_host_mismatch"})
            return result
        if profile in WRITER_PROFILES:
            identity_probe = _run(
                [
                    config.agent_git_command,
                    "-C",
                    str(checkout),
                    "commit",
                    "--allow-empty",
                    "-m",
                    "chore: verify Hollysys profile Git identity",
                ],
                env=env,
                timeout=config.preflight_command_timeout_seconds,
            )
            if identity_probe.returncode != 0:
                result.update(
                    {"ok": False, "error_code": "git_identity_invalid"}
                )
                return result
        canary_ref = (
            f"refs/heads/hollysys-preflight/{profile}/"
            f"{int(time.time())}"
        )
        push = _run(
            [
                config.agent_git_command,
                "-C",
                str(checkout),
                "push",
                "--dry-run",
                "origin",
                f"HEAD:{canary_ref}",
            ],
            env=env,
            timeout=max(30, config.preflight_command_timeout_seconds),
        )
        if profile in WRITER_PROFILES:
            result["repository_write_ok"] = push.returncode == 0
            if push.returncode != 0:
                result.update(
                    {
                        "ok": False,
                        "error_code": _failure_code(
                            push,
                            "repository_write_forbidden",
                        ),
                    }
                )
                return result
        else:
            locally_forbidden = (
                push.returncode == 67
                and "push_forbidden_for_role" in push.stderr
            )
            result["repository_write_ok"] = False
            result["reviewer_push_blocked"] = locally_forbidden
            if not locally_forbidden:
                result.update(
                    {"ok": False, "error_code": "push_forbidden_for_role"}
                )
                return result
    return result


def summarize_profile_preflight(
    config: ControllerConfig,
    *,
    deep: bool,
) -> dict:
    profiles = [
        profile_preflight(config, profile, deep=deep)
        for profile in sorted(ALL_PROFILES)
    ]
    fingerprints = [
        item.get("token_fingerprint")
        for item in profiles
        if item.get("token_fingerprint")
    ]
    duplicates = len(fingerprints) != len(set(fingerprints))
    dispatcher_fingerprint = next(
        (
            item.get("token_fingerprint")
            for item in profiles
            if item.get("profile") == "dispatcher"
        ),
        None,
    )
    controller_fingerprint: str | None = None
    try:
        controller_fingerprint = hashlib.sha256(
            config.read_token().encode("utf-8")
        ).hexdigest()
    except (OSError, ValueError):
        controller_fingerprint = None
    controller_matches_dispatcher = bool(
        controller_fingerprint
        and dispatcher_fingerprint
        and controller_fingerprint == dispatcher_fingerprint
    )
    auth_executable_fingerprints: list[str] = []
    for path in (
        Path(config.agent_git_command),
        Path(config.agent_git_command).with_name("gitlab-askpass"),
        Path(config.agent_git_command).with_name("gitlab-credential"),
        Path(config.agent_git_command).with_name("glab"),
        Path(config.agent_git_command).with_name("lark-cli"),
    ):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digest = "missing"
        auth_executable_fingerprints.append(f"{path.name}={digest}")
    contract_parts = [
        f"host={config.gitlab_base_url}",
        f"groups={','.join(config.allowed_groups)}",
        f"project={config.preflight_project_path}",
        *auth_executable_fingerprints,
        *[
            f"profile={item.get('profile')}:"
            f"{item.get('token_fingerprint') or item.get('error_code') or 'missing'}:"
            f"{item.get('_credential_scope') or 'invalid-scope'}"
            for item in sorted(profiles, key=lambda item: str(item["profile"]))
        ],
        f"controller={controller_fingerprint or 'missing'}",
    ]
    credential_contract_digest = hashlib.sha256(
        "\n".join(contract_parts).encode("utf-8")
    ).hexdigest()
    safe_profiles = [
        {
            key: value
            for key, value in item.items()
            if key not in {"token_fingerprint", "_credential_scope"}
        }
        for item in profiles
    ]
    return {
        "ok": all(item["ok"] for item in profiles)
        and not duplicates
        and controller_matches_dispatcher,
        "deep": deep,
        "profiles": safe_profiles,
        "unique_profile_tokens": not duplicates,
        "controller_token_matches_dispatcher": controller_matches_dispatcher,
        "controller_token_source": "dispatcher",
        # Internal activation binding. ControllerService removes this field
        # before returning or persisting any health/preflight report.
        "_credential_contract_digest": credential_contract_digest,
    }
