from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "container" / "sync-profile-skills.py"
SPEC = importlib.util.spec_from_file_location("sync_profile_skills", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
sync_profile_skills = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_profile_skills)


class ProfileSkillsSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.profiles_root = Path(self.temporary.name) / "profiles"
        for profile in sync_profile_skills.EXPECTED_PROFILES:
            profile_root = self.profiles_root / profile
            profile_root.mkdir(parents=True)
            (profile_root / "config.yaml").write_text("{}\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_synced_profile(self, profile: str) -> None:
        skill_root = self.profiles_root / profile / "skills" / "category" / "codex"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "---\nname: codex\ndescription: test\n---\n",
            encoding="utf-8",
        )
        manifest = (
            self.profiles_root
            / profile
            / "skills"
            / ".bundled_manifest"
        )
        manifest.write_text("codex:digest\n", encoding="utf-8")

    def test_sync_scopes_official_sync_to_every_profile(self) -> None:
        calls: list[subprocess.CompletedProcess[str]] = []

        def fake_run(command, **kwargs):
            profile_root = Path(kwargs["env"]["HERMES_HOME"])
            self._write_synced_profile(profile_root.name)
            calls.append(subprocess.CompletedProcess(command, 0))
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {"skipped_opt_out": False, "total_bundled": 1}
                ),
                stderr="",
            )

        with mock.patch.object(
            sync_profile_skills.subprocess, "run", side_effect=fake_run
        ):
            result = sync_profile_skills.sync_profiles(self.profiles_root)

        self.assertEqual(result, (12, 1))
        self.assertEqual(len(calls), 12)

    def test_opt_out_marker_is_a_contract_failure(self) -> None:
        marker = (
            self.profiles_root
            / "coder"
            / sync_profile_skills.OPT_OUT_MARKER
        )
        marker.write_text("disabled\n", encoding="utf-8")

        with self.assertRaisesRegex(
            sync_profile_skills.ProfileSkillsError,
            "disabled for Profile: coder",
        ):
            sync_profile_skills.sync_profiles(self.profiles_root)

    def test_curator_suppression_is_a_contract_failure(self) -> None:
        marker = (
            self.profiles_root
            / "coder"
            / "skills"
            / sync_profile_skills.SUPPRESSION_MARKER
        )
        marker.parent.mkdir(parents=True)
        marker.write_text("codex\n", encoding="utf-8")

        with self.assertRaisesRegex(
            sync_profile_skills.ProfileSkillsError,
            "suppressed for Profile: coder",
        ):
            sync_profile_skills.sync_profiles(self.profiles_root)

    def test_verify_rejects_inconsistent_manifests(self) -> None:
        for profile in sync_profile_skills.EXPECTED_PROFILES:
            self._write_synced_profile(profile)
        manifest = (
            self.profiles_root
            / "tester"
            / "skills"
            / ".bundled_manifest"
        )
        manifest.write_text("other:digest\n", encoding="utf-8")

        with self.assertRaisesRegex(
            sync_profile_skills.ProfileSkillsError,
            "missing for Profile tester",
        ):
            sync_profile_skills.verify_profiles(self.profiles_root)


if __name__ == "__main__":
    unittest.main()
