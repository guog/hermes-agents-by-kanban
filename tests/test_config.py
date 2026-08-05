from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from hollysys_controller.config import (
    ControllerConfig,
    normalize_gitlab_endpoint,
    trusted_runtime_uids,
)
from tests.helpers import config, write_profile_env


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_controller_uses_private_dispatcher_token_mirror(self) -> None:
        write_profile_env(
            self.config,
            token="secret",
        )
        token_file = self.config.controller_token_file
        token_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.config.read_token()

        token_file.chmod(0o600)
        self.assertEqual(
            self.config.read_token(),
            "secret",
        )

    def test_controller_rejects_token_file_symlink(self) -> None:
        write_profile_env(self.config, token="secret")
        token_file = self.config.controller_token_file
        target = self.root / "actual-controller-token"
        token_file.replace(target)
        token_file.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "token_file_invalid"):
            self.config.read_token()

    def test_compose_puid_is_a_trusted_runtime_owner(self) -> None:
        with patch.dict(
            "os.environ",
            {"PUID": "12345"},
            clear=True,
        ):
            self.assertIn(12345, trusted_runtime_uids())

    def test_locked_agent_clis_use_canonical_login_shell_paths(self) -> None:
        defaults = ControllerConfig.model_fields
        self.assertEqual(
            defaults["agent_git_command"].default,
            "/usr/local/bin/git",
        )
        self.assertEqual(
            defaults["glab_command"].default,
            "/usr/local/bin/glab",
        )
        self.assertEqual(
            defaults["lark_command"].default,
            "/usr/local/bin/lark-cli",
        )
        self.assertEqual(
            defaults["preflight_command_timeout_seconds"].default,
            120,
        )
        self.assertEqual(
            defaults["start_request_sync_timeout_seconds"].default,
            10.0,
        )
        self.assertEqual(defaults["worker_slow_warning_seconds"].default, 900)
        self.assertEqual(
            defaults["worker_progress_lease_seconds"].default,
            1800,
        )
        self.assertEqual(
            defaults["worker_heartbeat_stale_seconds"].default,
            300,
        )
        self.assertEqual(defaults["worker_redispatch_limit"].default, 2)

    def test_gitlab_endpoint_rejects_unsafe_or_mistyped_origins(self) -> None:
        valid = normalize_gitlab_endpoint(
            "https://green-git.hollysys.net"
        )
        self.assertEqual(valid.hostname, "green-git.hollysys.net")
        for invalid in (
            "ttps://green-git.hollysys.net",
            "http://green-git.hollysys.net",
            "https://green-git.hollysys.net/path",
            "https://token@green-git.hollysys.net",
            "https://other.example:8443",
            "https://gitlab.example.com",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_gitlab_endpoint(invalid)

    def test_allowed_groups_and_preflight_project_are_fail_closed(self) -> None:
        data = self.config.model_dump()
        data["allowed_groups"] = ["../outside"]
        with self.assertRaises(ValidationError):
            ControllerConfig.model_validate(data)
        data["allowed_groups"] = ["group"]
        data["preflight_project_path"] = "other/project"
        with self.assertRaises(ValidationError):
            ControllerConfig.model_validate(data)
        for unsafe in (
            "group/../outside",
            "group/project.git",
            "group/project?ref=main",
            "group",
        ):
            with self.subTest(unsafe=unsafe):
                data["preflight_project_path"] = unsafe
                with self.assertRaisesRegex(
                    ValidationError,
                    "preflight_project_path must be a canonical",
                ):
                    ControllerConfig.model_validate(data)


if __name__ == "__main__":
    unittest.main()
