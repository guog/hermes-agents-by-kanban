#!/usr/bin/env python3
"""Install safe command wrappers for fleet-managed Hermes profiles."""

from __future__ import annotations

import argparse
import grp
import os
import pathlib
import pwd
import re
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterable


PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ProfileCommandError(ValueError):
    pass


def _require_real_directory(path: pathlib.Path, *, label: str) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError as exc:
        raise ProfileCommandError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ProfileCommandError(
            f"{label} must be a real directory, not a symbolic link: {path}"
        )


def _require_executable(path: pathlib.Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ProfileCommandError(f"{label} must be an absolute path")
    try:
        status = path.stat()
    except FileNotFoundError as exc:
        raise ProfileCommandError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(status.st_mode) or not os.access(path, os.X_OK):
        raise ProfileCommandError(f"{label} must be an executable file: {path}")


def _wrapper_text(
    profile: str,
    *,
    runtime_user: str,
    runtime_uid: int,
    base_home: pathlib.Path,
    hermes_executable: pathlib.Path,
    setuidgid_executable: pathlib.Path,
) -> str:
    quoted_home = shlex.quote(str(base_home))
    quoted_hermes = shlex.quote(str(hermes_executable))
    quoted_setuidgid = shlex.quote(str(setuidgid_executable))
    quoted_user = shlex.quote(runtime_user)
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "umask 077\n"
        "current_uid=$(/usr/bin/id -u)\n"
        f"if [ \"${{current_uid}}\" = {runtime_uid} ]; then\n"
        f"  export HOME={quoted_home}\n"
        f"  export HERMES_HOME={quoted_home}\n"
        f"  exec {quoted_hermes} -p {profile} \"$@\"\n"
        "fi\n"
        "if [ \"${current_uid}\" = 0 ]; then\n"
        f"  exec {quoted_setuidgid} {quoted_user} /usr/bin/env "
        f"HOME={quoted_home} HERMES_HOME={quoted_home} "
        f"{quoted_hermes} -p {profile} \"$@\"\n"
        "fi\n"
        f"echo \"{profile}: run this command as root or {runtime_user}\" >&2\n"
        "exit 126\n"
    )


def install_profile_commands(
    profiles: Iterable[str],
    *,
    output_dir: pathlib.Path,
    runtime_user: str,
    owner: str,
    group: str,
    base_home: pathlib.Path,
    hermes_executable: pathlib.Path,
    setuidgid_executable: pathlib.Path,
) -> list[pathlib.Path]:
    profile_names = list(profiles)
    if not profile_names:
        raise ProfileCommandError("at least one profile is required")
    if len(profile_names) != len(set(profile_names)):
        raise ProfileCommandError("profile names must be unique")
    for profile in profile_names:
        if not PROFILE_RE.fullmatch(profile):
            raise ProfileCommandError(f"invalid profile name: {profile!r}")

    try:
        runtime_uid = pwd.getpwnam(runtime_user).pw_uid
        owner_uid = pwd.getpwnam(owner).pw_uid
        owner_gid = grp.getgrnam(group).gr_gid
    except KeyError as exc:
        raise ProfileCommandError(
            f"runtime user, file owner or group does not exist: {exc}"
        ) from exc

    _require_real_directory(base_home, label="Hermes data root")
    _require_executable(hermes_executable, label="Hermes CLI")
    _require_executable(setuidgid_executable, label="s6-setuidgid")

    if output_dir.exists() or output_dir.is_symlink():
        _require_real_directory(output_dir, label="profile command directory")
    else:
        output_dir.mkdir(mode=0o755, parents=True)
    os.chmod(output_dir, 0o755)
    current = output_dir.stat()
    if current.st_uid != owner_uid or current.st_gid != owner_gid:
        os.chown(output_dir, owner_uid, owner_gid)

    installed: list[pathlib.Path] = []
    for profile in profile_names:
        destination = output_dir / profile
        if destination.exists() or destination.is_symlink():
            status = destination.lstat()
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
                raise ProfileCommandError(
                    f"existing profile command must be a regular file: {destination}"
                )
        payload = _wrapper_text(
            profile,
            runtime_user=runtime_user,
            runtime_uid=runtime_uid,
            base_home=base_home,
            hermes_executable=hermes_executable,
            setuidgid_executable=setuidgid_executable,
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{profile}.",
            dir=output_dir,
            text=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o755)
                os.fchown(stream.fileno(), owner_uid, owner_gid)
            os.replace(temporary_name, destination)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
        installed.append(destination)
    return installed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=pathlib.Path, required=True)
    parser.add_argument("--runtime-user", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--base-home", type=pathlib.Path, required=True)
    parser.add_argument("--hermes-cli", type=pathlib.Path, required=True)
    parser.add_argument("--setuidgid", type=pathlib.Path, required=True)
    parser.add_argument("--profiles", nargs="+", required=True)
    args = parser.parse_args()
    try:
        installed = install_profile_commands(
            args.profiles,
            output_dir=args.output_dir,
            runtime_user=args.runtime_user,
            owner=args.owner,
            group=args.group,
            base_home=args.base_home,
            hermes_executable=args.hermes_cli,
            setuidgid_executable=args.setuidgid,
        )
    except (OSError, ProfileCommandError) as exc:
        print(f"profile command installation: {exc}", file=sys.stderr)
        return 65
    print(f"profile command installation: installed {len(installed)} managed commands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
