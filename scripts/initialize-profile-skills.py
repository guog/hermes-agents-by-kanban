#!/usr/bin/env python3
"""Initialize or adopt a profile's persistent local Skills without overwrites."""

from __future__ import annotations

import argparse
import grp
import hashlib
import json
import os
import pathlib
import pwd
import re
import shutil
import tempfile
from typing import Any


MARKER_SCHEMA_VERSION = 1
PROFILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ROLE_SKILL_PATTERN = re.compile(r"^sdd-[a-z0-9][a-z0-9._-]*$")


class SkillInitializationError(ValueError):
    """Raised when the persistent Skill boundary cannot be established safely."""


def _require_directory(path: pathlib.Path, label: str) -> None:
    if path.is_symlink():
        raise SkillInitializationError(f"{label} must not be a symbolic link: {path}")
    if not path.is_dir():
        raise SkillInitializationError(f"{label} must be a directory: {path}")


def _require_regular_file(path: pathlib.Path, label: str) -> None:
    if path.is_symlink():
        raise SkillInitializationError(f"{label} must not be a symbolic link: {path}")
    if not path.is_file():
        raise SkillInitializationError(f"{label} must be a regular file: {path}")


def _reject_symlinks(root: pathlib.Path, label: str) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise SkillInitializationError(
                f"{label} must not contain symbolic links: {path}"
            )


def _source_role_skill(source: pathlib.Path) -> pathlib.Path:
    _require_directory(source, "bundled Skill source")
    candidates = []
    for path in sorted(source.iterdir()):
        if (
            ROLE_SKILL_PATTERN.fullmatch(path.name)
            and path.is_dir()
            and not path.is_symlink()
        ):
            skill_file = path / "SKILL.md"
            if skill_file.is_file() and not skill_file.is_symlink():
                candidates.append(path)
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise SkillInitializationError(
            "bundled Skill source must contain exactly one sdd-* role Skill "
            f"with SKILL.md; found {names}"
        )
    _reject_symlinks(candidates[0], "bundled role Skill")
    return candidates[0]


def _local_role_skills(target: pathlib.Path) -> list[pathlib.Path]:
    result = []
    for path in sorted(target.iterdir()):
        if not ROLE_SKILL_PATTERN.fullmatch(path.name):
            continue
        if path.is_symlink() or not path.is_dir():
            raise SkillInitializationError(
                f"local role Skill must be a real directory: {path}"
            )
        skill_file = path / "SKILL.md"
        _require_regular_file(skill_file, "local role Skill entrypoint")
        result.append(path)
    return result


def _tree_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise SkillInitializationError(
                f"Skill tree must not contain symbolic links: {path}"
            )
        if path.is_dir():
            digest.update(f"D\0{relative}\0".encode())
        elif path.is_file():
            digest.update(f"F\0{relative}\0".encode())
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise SkillInitializationError(
                f"Skill tree contains an unsupported filesystem entry: {path}"
            )
    return digest.hexdigest()


def _resolve_identity(owner: str | None, group: str | None) -> tuple[int, int]:
    uid = os.geteuid() if owner is None else pwd.getpwnam(owner).pw_uid
    gid = os.getegid() if group is None else grp.getgrnam(group).gr_gid
    return uid, gid


def _chown_tree(root: pathlib.Path, uid: int, gid: int) -> None:
    os.chown(root, uid, gid, follow_symlinks=False)
    for path in root.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)


def _seed_role_skill(
    source_role: pathlib.Path,
    target: pathlib.Path,
    *,
    uid: int,
    gid: int,
) -> None:
    destination = target / source_role.name
    with tempfile.TemporaryDirectory(
        prefix=f".{target.name}-seed-", dir=target.parent
    ) as temporary:
        staging = pathlib.Path(temporary) / source_role.name
        shutil.copytree(source_role, staging)
        _chown_tree(staging, uid, gid)
        try:
            staging.rename(destination)
        except FileExistsError as exc:
            raise SkillInitializationError(
                f"local role Skill appeared concurrently; nothing was overwritten: "
                f"{destination}"
            ) from exc


def _marker_payload(profile: str, role_name: str) -> dict[str, Any]:
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "profile": profile,
        "role_skill": role_name,
    }


def _write_marker(marker: pathlib.Path, payload: dict[str, Any]) -> None:
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _require_directory(marker.parent, "Skill initialization marker directory")
    os.chmod(marker.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.", dir=marker.parent
    )
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, marker)
        except FileExistsError as exc:
            raise SkillInitializationError(
                f"Skill initialization marker appeared concurrently; "
                f"nothing was overwritten: {marker}"
            ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_marker(marker: pathlib.Path, profile: str) -> dict[str, Any]:
    _require_regular_file(marker, "Skill initialization marker")
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillInitializationError(
            f"Skill initialization marker is invalid: {marker}"
        ) from exc
    expected_keys = {"schema_version", "profile", "role_skill"}
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise SkillInitializationError(
            f"Skill initialization marker has an unsupported shape: {marker}"
        )
    if payload.get("schema_version") != MARKER_SCHEMA_VERSION:
        raise SkillInitializationError(
            f"Skill initialization marker schema is unsupported: {marker}"
        )
    if payload.get("profile") != profile:
        raise SkillInitializationError(
            f"Skill initialization marker profile mismatch: {marker}"
        )
    role_name = payload.get("role_skill")
    if not isinstance(role_name, str) or not ROLE_SKILL_PATTERN.fullmatch(role_name):
        raise SkillInitializationError(
            f"Skill initialization marker role_skill is invalid: {marker}"
        )
    return payload


def _validate_recorded_role(target: pathlib.Path, role_name: str) -> None:
    role = target / role_name
    if role.is_symlink() or not role.is_dir():
        raise SkillInitializationError(
            f"persistent role Skill is missing; restore it before startup: {role}"
        )
    _require_regular_file(role / "SKILL.md", "persistent role Skill entrypoint")


def initialize_profile_skills(
    *,
    profile: str,
    source: pathlib.Path,
    target: pathlib.Path,
    marker_root: pathlib.Path,
    new_profile: bool,
    uid: int | None = None,
    gid: int | None = None,
) -> str:
    """Seed a new profile or adopt existing Skills, then validate on every boot."""
    if not PROFILE_PATTERN.fullmatch(profile):
        raise SkillInitializationError(f"invalid profile name: {profile!r}")
    _require_directory(target, "persistent local Skill directory")
    _require_directory(marker_root, "Skill initialization marker root")
    marker = marker_root / profile
    owner_uid = os.geteuid() if uid is None else uid
    owner_gid = os.getegid() if gid is None else gid

    if marker.exists() or marker.is_symlink():
        payload = _read_marker(marker, profile)
        _validate_recorded_role(target, payload["role_skill"])
        return "preserved"

    source_role = _source_role_skill(source)
    local_roles = _local_role_skills(target)
    if new_profile:
        entries = list(target.iterdir())
        if not entries:
            _seed_role_skill(
                source_role,
                target,
                uid=owner_uid,
                gid=owner_gid,
            )
        elif (
            len(entries) == 1
            and len(local_roles) == 1
            and local_roles[0].name == source_role.name
            and _tree_digest(local_roles[0]) == _tree_digest(source_role)
        ):
            # Resume safely if the atomic directory rename completed but the
            # process stopped before the marker rename.
            pass
        else:
            raise SkillInitializationError(
                "new profile local Skill directory is not empty; refusing to "
                f"overwrite or adopt it without an existing profile: {target}"
            )
        action = "seeded"
    else:
        if len(local_roles) != 1 or local_roles[0].name != source_role.name:
            names = ", ".join(path.name for path in local_roles) or "none"
            raise SkillInitializationError(
                "existing profile migration requires exactly the bundled "
                f"role Skill {source_role.name!r}; found {names}. Restore the "
                "persistent role Skill before startup."
            )
        action = "adopted"

    _validate_recorded_role(target, source_role.name)
    _write_marker(marker, _marker_payload(profile, source_role.name))
    return action


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Seed or adopt one Profile's persistent local Skills"
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--source", required=True, type=pathlib.Path)
    parser.add_argument("--target", required=True, type=pathlib.Path)
    parser.add_argument("--marker-root", required=True, type=pathlib.Path)
    parser.add_argument("--new-profile", action="store_true")
    parser.add_argument("--owner")
    parser.add_argument("--group")
    args = parser.parse_args()
    try:
        uid, gid = _resolve_identity(args.owner, args.group)
        action = initialize_profile_skills(
            profile=args.profile,
            source=args.source,
            target=args.target,
            marker_root=args.marker_root,
            new_profile=args.new_profile,
            uid=uid,
            gid=gid,
        )
    except (KeyError, OSError, SkillInitializationError) as exc:
        parser.exit(65, f"profile Skill initialization: {exc}\n")
    print(f"profile Skill initialization: {args.profile}: {action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
