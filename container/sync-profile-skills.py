#!/usr/bin/env python3
"""Seed and verify Hermes bundled Skills for every Hollysys Agent Profile."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

EXPECTED_PROFILES = (
    "code-reviewer",
    "coder",
    "dispatcher",
    "fde",
    "plan-reviewer",
    "planner",
    "prd-writer",
    "spec-reviewer",
    "spec-writer",
    "task-reviewer",
    "tasker",
    "tester",
)
OPT_OUT_MARKER = ".no-bundled-skills"
SUPPRESSION_MARKER = ".curator_suppressed"
SYNC_PROGRAM = """
import json
from tools.skills_sync import sync_skills

result = sync_skills(quiet=True)
print(json.dumps({
    "skipped_opt_out": bool(result.get("skipped_opt_out")),
    "total_bundled": int(result.get("total_bundled", 0)),
}, sort_keys=True))
"""


class ProfileSkillsError(RuntimeError):
    """A Profile cannot satisfy the bundled-Skills contract."""


def _profile_roots(profiles_root: Path) -> list[Path]:
    if profiles_root.is_symlink() or not profiles_root.is_dir():
        raise ProfileSkillsError(
            f"Profile root must be a regular directory: {profiles_root}"
        )

    roots: list[Path] = []
    for profile in EXPECTED_PROFILES:
        profile_root = profiles_root / profile
        config_path = profile_root / "config.yaml"
        if profile_root.is_symlink() or not profile_root.is_dir():
            raise ProfileSkillsError(f"Missing regular Profile directory: {profile}")
        if config_path.is_symlink() or not config_path.is_file():
            raise ProfileSkillsError(f"Missing regular Profile config: {profile}")
        marker = profile_root / OPT_OUT_MARKER
        if marker.exists() or marker.is_symlink():
            raise ProfileSkillsError(
                f"Bundled Skills are disabled for Profile: {profile}"
            )
        suppression = profile_root / "skills" / SUPPRESSION_MARKER
        if suppression.exists() or suppression.is_symlink():
            raise ProfileSkillsError(
                f"Bundled Skills are suppressed for Profile: {profile}"
            )
        roots.append(profile_root)
    return roots


def _manifest_names(profile_root: Path) -> set[str]:
    manifest = profile_root / "skills" / ".bundled_manifest"
    if manifest.is_symlink() or not manifest.is_file():
        raise ProfileSkillsError(
            f"Bundled-Skills manifest is missing for Profile: {profile_root.name}"
        )
    names = {
        line.partition(":")[0].strip()
        for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    if not names:
        raise ProfileSkillsError(
            f"Bundled-Skills manifest is empty for Profile: {profile_root.name}"
        )
    return names


def _installed_skill_names(profile_root: Path) -> set[str]:
    skills_root = profile_root / "skills"
    names: set[str] = set()
    for skill_file in skills_root.rglob("SKILL.md"):
        fallback = skill_file.parent.name
        name = fallback
        try:
            lines = skill_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                stripped = line.strip()
                if stripped == "---":
                    break
                if stripped.startswith("name:"):
                    candidate = stripped.split(":", 1)[1].strip().strip("\"'")
                    if candidate:
                        name = candidate
                    break
        names.add(name)
    return names


def verify_profiles(profiles_root: Path) -> tuple[int, int]:
    reference: set[str] | None = None
    for profile_root in _profile_roots(profiles_root):
        manifest_names = _manifest_names(profile_root)
        installed_names = _installed_skill_names(profile_root)
        missing = manifest_names - installed_names
        if missing:
            raise ProfileSkillsError(
                "Bundled Skills are missing for Profile "
                f"{profile_root.name}: {len(missing)}"
            )
        if reference is None:
            reference = manifest_names
        elif manifest_names != reference:
            raise ProfileSkillsError(
                "Bundled-Skills manifests differ for Profile: "
                f"{profile_root.name}"
            )
    return len(EXPECTED_PROFILES), len(reference or ())


def sync_profiles(
    profiles_root: Path,
    *,
    python_executable: str = sys.executable,
    timeout_seconds: int = 120,
) -> tuple[int, int]:
    expected_total: int | None = None
    for profile_root in _profile_roots(profiles_root):
        environment = os.environ.copy()
        environment.update(
            {
                "HERMES_HOME": str(profile_root),
                "HERMES_PROFILE": profile_root.name,
            }
        )
        try:
            completed = subprocess.run(
                [python_executable, "-c", SYNC_PROGRAM],
                env=environment,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProfileSkillsError(
                f"Bundled-Skills sync failed for Profile: {profile_root.name}"
            ) from exc
        if completed.returncode != 0:
            raise ProfileSkillsError(
                "Bundled-Skills sync returned a failure for Profile: "
                f"{profile_root.name}"
            )
        output_lines = [
            line for line in completed.stdout.splitlines() if line.strip()
        ]
        try:
            result = json.loads(output_lines[-1])
        except (IndexError, json.JSONDecodeError, TypeError) as exc:
            raise ProfileSkillsError(
                "Bundled-Skills sync returned invalid output for Profile: "
                f"{profile_root.name}"
            ) from exc
        total_bundled = result.get("total_bundled")
        if result.get("skipped_opt_out") or not isinstance(total_bundled, int):
            raise ProfileSkillsError(
                f"Bundled-Skills sync was skipped for Profile: {profile_root.name}"
            )
        if total_bundled <= 0:
            raise ProfileSkillsError(
                "Hermes bundled-Skills catalog is empty for Profile: "
                f"{profile_root.name}"
            )
        if expected_total is None:
            expected_total = total_bundled
        elif total_bundled != expected_total:
            raise ProfileSkillsError(
                "Hermes bundled-Skills catalog changed during Profile sync"
            )

    profile_count, manifest_count = verify_profiles(profiles_root)
    if expected_total != manifest_count:
        raise ProfileSkillsError(
            "Bundled-Skills manifest does not cover the Hermes catalog: "
            f"catalog={expected_total}, manifest={manifest_count}"
        )
    return profile_count, manifest_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profiles-root",
        type=Path,
        default=Path(
            os.environ.get("HOLLYSYS_PROFILES_ROOT", "/opt/data/profiles")
        ),
    )
    parser.add_argument("--verify-only", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.verify_only:
            profile_count, bundled_count = verify_profiles(args.profiles_root)
            action = "verified"
        else:
            profile_count, bundled_count = sync_profiles(args.profiles_root)
            action = "synchronized"
    except ProfileSkillsError as exc:
        print(f"sync-profile-skills: ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "sync-profile-skills: "
        f"{action} profiles={profile_count} bundled_skills={bundled_count}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
