from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


class HermesPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        patch_path = (
            Path(__file__).resolve().parent.parent
            / "container"
            / "patch-hermes-terminal.py"
        )
        spec = importlib.util.spec_from_file_location(
            "hollysys_hermes_patch",
            patch_path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cls.patch = module

    def test_active_profile_prompt_uses_the_profile_home_once(self) -> None:
        source = (
            "    else:\n"
            "        stable_parts.append(\n"
            '            f"Active Hermes profile: {active_profile}. This session reads "\n'
            '            f"and writes {get_hermes_home()}/profiles/{active_profile}/. The default "\n'
            '            f"profile\'s data lives at {get_hermes_home()}/skills/, {get_hermes_home()}/plugins/, "\n'
            '            f"{get_hermes_home()}/cron/, {get_hermes_home()}/memories/ — those belong to a "\n'
        )

        patched = self.patch.patch_system_prompt(source)

        self.assertIn('f"and writes {get_hermes_home()}/. The default "', patched)
        self.assertNotIn(
            "get_hermes_home()}/profiles/{active_profile}",
            patched,
        )
        self.assertIn("get_default_hermes_root", patched)

