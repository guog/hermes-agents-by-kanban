from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from hollysys_controller.git_auth import (
    ALL_PROFILES,
    ProfileCredential,
    _failure_code,
    _profile_token_is_persisted,
    _safe_env,
    profile_credential,
    profile_preflight,
    read_profile_env,
    summarize_profile_preflight,
)
from tests.helpers import config


class GitAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = Path(__file__).resolve().parents[1]
        self.wrapper = self.repository / "container" / "git" / "git"
        self.askpass = (
            self.repository / "container" / "git" / "gitlab-askpass"
        )
        self.credential = (
            self.repository / "container" / "git" / "gitlab-credential"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _base_env() -> dict[str, str]:
        env = os.environ.copy()
        for key in (
            "HERMES_PROFILE",
            "GITLAB_HOST",
            "GITLAB_ALLOWED_GROUPS",
            "GITLAB_TOKEN",
            "GIT_TRACE",
            "GIT_TRACE_PACKET",
            "GIT_TRACE_CURL",
            "GIT_CURL_VERBOSE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_NOSYSTEM",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_EXEC_PATH",
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_PROXY_COMMAND",
            "GIT_SSL_NO_VERIFY",
            "GIT_PROTOCOL_FROM_USER",
            "GIT_ALLOW_PROTOCOL",
        ):
            env.pop(key, None)
        return env

    def _run_wrapper(
        self,
        *arguments: str,
        profile: str | None = None,
        groups: str = "group",
    ) -> subprocess.CompletedProcess[str]:
        env = self._base_env()
        if profile:
            env.update(
                {
                    "HERMES_PROFILE": profile,
                    "GITLAB_HOST": "https://green-git.hollysys.net",
                    "GITLAB_ALLOWED_GROUPS": groups,
                    "GITLAB_TOKEN": "safe-test-token",
                }
            )
        return subprocess.run(
            [str(self.wrapper), *arguments],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_wrapper_fails_closed_before_network_access(self) -> None:
        ssh = self._run_wrapper(
            "ls-remote",
            "git@green-git.hollysys.net:group/project.git",
        )
        self.assertEqual(ssh.returncode, 68)
        self.assertIn("error=ssh_disabled", ssh.stderr)
        scp_without_user = self._run_wrapper(
            "ls-remote",
            "green-git.hollysys.net:group/project.git",
        )
        self.assertEqual(scp_without_user.returncode, 68)
        self.assertIn("error=ssh_disabled", scp_without_user.stderr)

        missing = self._run_wrapper(
            "ls-remote",
            "https://green-git.hollysys.net/group/project.git",
        )
        self.assertEqual(missing.returncode, 64)
        self.assertIn("error=missing_profile_identity", missing.stderr)

        foreign = self._run_wrapper(
            "ls-remote",
            "https://gitlab.example.com/group/project.git",
            profile="coder",
        )
        self.assertEqual(foreign.returncode, 70)
        self.assertIn("error=remote_host_mismatch", foreign.stderr)

        outside = self._run_wrapper(
            "ls-remote",
            "https://green-git.hollysys.net/other/project.git",
            profile="coder",
        )
        self.assertEqual(outside.returncode, 69)
        self.assertIn("error=project_access_denied", outside.stderr)
        self.assertIn("project=other/project", outside.stderr)
        self.assertIn("operation=ls-remote", outside.stderr)
        traversal = self._run_wrapper(
            "ls-remote",
            "https://green-git.hollysys.net/group/../other/project.git",
            profile="coder",
        )
        self.assertEqual(traversal.returncode, 69)
        self.assertIn("error=project_access_denied", traversal.stderr)

        override = self._run_wrapper(
            "-c",
            "alias.publish=push",
            "publish",
            "https://green-git.hollysys.net/group/project.git",
            profile="coder",
        )
        self.assertEqual(override.returncode, 71)
        self.assertIn("error=git_config_override_forbidden", override.stderr)

    def test_wrapper_preserves_local_git_options(self) -> None:
        checkout = self.root / "local"
        subprocess.run(
            ["/usr/bin/git", "init", str(checkout)],
            text=True,
            capture_output=True,
            check=True,
        )
        switched = self._run_wrapper(
            "-C",
            str(checkout),
            "switch",
            "-c",
            "local-only-branch",
        )
        self.assertEqual(switched.returncode, 0, switched.stderr)
        branch = subprocess.run(
            ["/usr/bin/git", "-C", str(checkout), "branch", "--show-current"],
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertEqual(branch.stdout.strip(), "local-only-branch")

    def test_reviewer_push_is_rejected_even_with_a_token(self) -> None:
        result = self._run_wrapper(
            "-C",
            str(self.repository),
            "push",
            "--dry-run",
            "https://green-git.hollysys.net/group/project.git",
            "HEAD:refs/heads/test",
            profile="tester",
        )
        self.assertEqual(result.returncode, 67)
        self.assertIn("error=push_forbidden_for_role", result.stderr)
        self.assertNotIn("safe-test-token", result.stderr)

        checkout = self.root / "checkout"
        subprocess.run(
            ["/usr/bin/git", "init", str(checkout)],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(checkout),
                "remote",
                "add",
                "origin",
                "https://green-git.hollysys.net/group/project.git",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        named_remote = self._run_wrapper(
            f"-C{checkout}",
            "push",
            "--dry-run",
            "origin",
            "HEAD:refs/heads/test",
            profile="code-reviewer",
        )
        self.assertEqual(named_remote.returncode, 67)
        self.assertIn(
            "error=push_forbidden_for_role",
            named_remote.stderr,
        )
        subprocess.run(
            [
                "/usr/bin/git",
                "-C",
                str(checkout),
                "remote",
                "set-url",
                "--push",
                "origin",
                "https://gitlab.example.com/group/project.git",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        foreign_push_url = self._run_wrapper(
            f"-C{checkout}",
            "push",
            "--dry-run",
            "origin",
            "HEAD:refs/heads/test",
            profile="coder",
        )
        self.assertEqual(foreign_push_url.returncode, 70)
        self.assertIn("error=remote_host_mismatch", foreign_push_url.stderr)
        context_override = self._run_wrapper(
            f"--git-dir={checkout / '.git'}",
            "push",
            "--dry-run",
            "origin",
            "HEAD:refs/heads/test",
            profile="coder",
        )
        self.assertEqual(context_override.returncode, 71)
        self.assertIn(
            "error=git_context_override_forbidden",
            context_override.stderr,
        )

    def test_url_rewrite_configuration_is_rejected_for_network_git(self) -> None:
        env = self._base_env()
        env.update(
            {
                "HERMES_PROFILE": "coder",
                "GITLAB_HOST": "https://green-git.hollysys.net",
                "GITLAB_ALLOWED_GROUPS": "group",
                "GITLAB_TOKEN": "safe-test-token",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "url.https://gitlab.example.com/.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://green-git.hollysys.net/",
            }
        )

        result = subprocess.run(
            [
                str(self.wrapper),
                "ls-remote",
                "https://green-git.hollysys.net/group/project.git",
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 71)
        self.assertIn("error=git_url_rewrite_forbidden", result.stderr)

    def test_network_git_rejects_environment_and_credential_overrides(
        self,
    ) -> None:
        for injected in (
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "!malicious-helper",
            },
            {"GIT_EXEC_PATH": str(self.root / "malicious-exec-path")},
            {"GIT_SSL_NO_VERIFY": "true"},
        ):
            with self.subTest(injected=tuple(injected)):
                env = self._base_env()
                env.update(
                    {
                        "HERMES_PROFILE": "coder",
                        "GITLAB_HOST": "https://green-git.hollysys.net",
                        "GITLAB_ALLOWED_GROUPS": "group",
                        "GITLAB_TOKEN": "safe-test-token",
                        **injected,
                    }
                )
                result = subprocess.run(
                    [
                        str(self.wrapper),
                        "ls-remote",
                        "https://green-git.hollysys.net/group/project.git",
                    ],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertEqual(result.returncode, 71)
                self.assertIn(
                    "error=git_environment_override_forbidden",
                    result.stderr,
                )

    def test_askpass_and_credential_helper_use_environment_only(self) -> None:
        env = self._base_env()
        env["GITLAB_TOKEN"] = "safe-test-token"
        username = subprocess.run(
            [str(self.askpass), "Username for https://green-git.hollysys.net"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        password = subprocess.run(
            [str(self.askpass), "Password for https://green-git.hollysys.net"],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        helper = subprocess.run(
            [str(self.credential), "get"],
            input=(
                "protocol=https\n"
                "host=green-git.hollysys.net\n"
                "path=group/project.git\n\n"
            ),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(username.stdout, "oauth2\n")
        self.assertEqual(password.stdout, "safe-test-token\n")
        self.assertIn("username=oauth2\n", helper.stdout)
        self.assertIn("password=safe-test-token\n", helper.stdout)

    def test_preflight_environment_does_not_inherit_controller_secrets(self) -> None:
        credential = ProfileCredential(
            profile="tester",
            host_url="https://green-git.hollysys.net",
            allowed_groups=("group",),
            token="profile-token",
            env_path=self.root / ".env",
            home=self.root / "home",
        )
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "OPENAI_API_KEY": "controller-provider-secret",
                "GLAB_TOKEN": "wrong-controller-token",
                "GIT_TRACE_CURL": "1",
            },
            clear=True,
        ):
            env = _safe_env(credential)
        self.assertEqual(env["GITLAB_TOKEN"], "profile-token")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("GLAB_TOKEN", env)
        self.assertNotIn("GIT_TRACE_CURL", env)

    def test_empty_glab_token_placeholder_is_not_persisted_credential(
        self,
    ) -> None:
        home = self.root / "home"
        config_path = home / ".config" / "glab-cli" / "config.yml"
        config_path.parent.mkdir(parents=True)
        credential = ProfileCredential(
            profile="dispatcher",
            host_url="https://green-git.hollysys.net",
            allowed_groups=("group",),
            token="profile-token",
            env_path=self.root / ".env",
            home=home,
        )
        config_path.write_text(
            "hosts:\n  gitlab.com:\n    token: null\n",
            encoding="utf-8",
        )

        self.assertFalse(_profile_token_is_persisted(credential))

        config_path.write_text(
            "hosts:\n  green-git.hollysys.net:\n    token: another-token\n",
            encoding="utf-8",
        )
        self.assertTrue(_profile_token_is_persisted(credential))

    def test_static_preflight_enforces_unique_profile_tokens(self) -> None:
        cfg = config(self.root)
        cfg.gitlab_host = "green-git.hollysys.net"
        cfg.profiles_root = self.root / "profiles"
        for index, profile in enumerate(sorted(ALL_PROFILES), start=1):
            profile_root = cfg.profiles_root / profile
            (profile_root / "home").mkdir(parents=True)
            env_file = profile_root / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        f"HERMES_PROFILE={profile}",
                        "GITLAB_HOST=https://green-git.hollysys.net",
                        "GITLAB_ALLOWED_GROUPS=group",
                        f"GITLAB_TOKEN=profile-token-{index}",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)

        result = summarize_profile_preflight(cfg, deep=False)
        self.assertTrue(result["ok"])
        self.assertTrue(result["unique_profile_tokens"])
        self.assertTrue(result["controller_token_matches_dispatcher"])
        self.assertEqual(result["controller_token_source"], "dispatcher")
        self.assertNotIn("token_fingerprint", str(result))

        duplicate = cfg.profiles_root / "tester" / ".env"
        duplicate.write_text(
            "HERMES_PROFILE=tester\n"
            "GITLAB_HOST=https://green-git.hollysys.net\n"
            "GITLAB_ALLOWED_GROUPS=group\n"
            "GITLAB_TOKEN=profile-token-1\n",
            encoding="utf-8",
        )
        duplicate.chmod(0o600)
        result = summarize_profile_preflight(cfg, deep=False)
        self.assertFalse(result["ok"])
        self.assertFalse(result["unique_profile_tokens"])

        wrapper_root = self.root / "auth-bin"
        wrapper_root.mkdir()
        cfg.agent_git_command = str(wrapper_root / "git")
        for name in (
            "git",
            "gitlab-askpass",
            "gitlab-credential",
            "glab",
            "lark-cli",
        ):
            executable = wrapper_root / name
            executable.write_text(f"{name}:v1\n", encoding="utf-8")
            executable.chmod(0o555)
        first_digest = summarize_profile_preflight(
            cfg,
            deep=False,
        )["_credential_contract_digest"]
        git_wrapper = wrapper_root / "git"
        git_wrapper.chmod(0o755)
        git_wrapper.write_text("git:v2\n", encoding="utf-8")
        git_wrapper.chmod(0o555)
        second_digest = summarize_profile_preflight(
            cfg,
            deep=False,
        )["_credential_contract_digest"]
        self.assertNotEqual(first_digest, second_digest)
        glab = wrapper_root / "glab"
        glab.chmod(0o755)
        glab.write_text("glab:v2\n", encoding="utf-8")
        glab.chmod(0o555)
        third_digest = summarize_profile_preflight(
            cfg,
            deep=False,
        )["_credential_contract_digest"]
        self.assertNotEqual(second_digest, third_digest)

    def test_missing_profile_env_has_a_stable_error_code(self) -> None:
        cfg = config(self.root)
        cfg.profiles_root = self.root / "profiles"

        result = profile_preflight(cfg, "coder", deep=False)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "missing_profile_identity")

    def test_profile_host_is_normalized_before_comparison(self) -> None:
        cfg = config(self.root)
        profile_root = cfg.profiles_root / "dispatcher"
        (profile_root / "home").mkdir(parents=True)
        env_file = profile_root / ".env"
        env_file.write_text(
            "HERMES_PROFILE=dispatcher\n"
            'GITLAB_HOST=\"green-git.hollysys.net\"\n'
            "GITLAB_ALLOWED_GROUPS=group\n"
            "GITLAB_TOKEN=profile-token\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)

        credential = profile_credential(cfg, "dispatcher")

        self.assertEqual(
            credential.host_url,
            "https://green-git.hollysys.net",
        )

        env_file.write_text(
            env_file.read_text(encoding="utf-8").replace(
                'GITLAB_HOST=\"green-git.hollysys.net\"',
                "GITLAB_HOST=http://green-git.hollysys.net",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "invalid_gitlab_host"):
            profile_credential(cfg, "dispatcher")

    def test_deep_preflight_checks_the_real_login_shell_cli_paths(self) -> None:
        cfg = config(self.root)
        cfg.profiles_root = self.root / "profiles"
        cfg.preflight_project_path = "group/project"
        login_bin = self.root / "login-bin"
        cfg.agent_git_command = str(login_bin / "git")
        profile_root = cfg.profiles_root / "coder"
        (profile_root / "home").mkdir(parents=True)
        env_file = profile_root / ".env"
        env_file.write_text(
            "HERMES_PROFILE=coder\n"
            "GITLAB_HOST=https://green-git.hollysys.net\n"
            "GITLAB_ALLOWED_GROUPS=group\n"
            "GITLAB_TOKEN=profile-token\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        commands = (
            subprocess.CompletedProcess(
                ["sh"],
                0,
                stdout=f"{login_bin / 'git'}\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["sh"],
                0,
                stdout=f"{login_bin / 'glab'}\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["sh"],
                0,
                stdout=f"{login_bin / 'lark-cli'}\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["glab"],
                1,
                stdout="",
                stderr="HTTP 401 unauthorized",
            ),
        )
        with patch(
            "hollysys_controller.git_auth._run",
            side_effect=commands,
        ) as run:
            result = profile_preflight(cfg, "coder", deep=True)

        self.assertTrue(result["git_path_ok"])
        self.assertTrue(result["glab_path_ok"])
        self.assertTrue(result["lark_cli_path_ok"])
        self.assertEqual(result["error_code"], "token_expired_or_revoked")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list[:3]],
            [
                ["/bin/sh", "-lc", "command -v git"],
                ["/bin/sh", "-lc", "command -v glab"],
                ["/bin/sh", "-lc", "command -v lark-cli"],
            ],
        )

    def test_deep_preflight_rejects_a_glab_path_outside_login_bin(self) -> None:
        cfg = config(self.root)
        cfg.profiles_root = self.root / "profiles"
        cfg.preflight_project_path = "group/project"
        login_bin = self.root / "login-bin"
        cfg.agent_git_command = str(login_bin / "git")
        profile_root = cfg.profiles_root / "coder"
        (profile_root / "home").mkdir(parents=True)
        env_file = profile_root / ".env"
        env_file.write_text(
            "HERMES_PROFILE=coder\n"
            "GITLAB_HOST=https://green-git.hollysys.net\n"
            "GITLAB_ALLOWED_GROUPS=group\n"
            "GITLAB_TOKEN=profile-token\n",
            encoding="utf-8",
        )
        env_file.chmod(0o600)
        commands = (
            subprocess.CompletedProcess(
                ["sh"],
                0,
                stdout=f"{login_bin / 'git'}\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["sh"],
                0,
                stdout="/opt/cli/bin/glab\n",
                stderr="",
            ),
        )
        with patch(
            "hollysys_controller.git_auth._run",
            side_effect=commands,
        ):
            result = profile_preflight(cfg, "coder", deep=True)

        self.assertFalse(result["ok"])
        self.assertFalse(result["glab_path_ok"])
        self.assertEqual(result["error_code"], "glab_not_on_path")

    def test_transport_403_preserves_read_write_diagnosis(self) -> None:
        rejected = subprocess.CompletedProcess(
            ["git"],
            1,
            stdout="",
            stderr="remote: HTTP 403 forbidden",
        )

        self.assertEqual(
            _failure_code(rejected, "repository_read_forbidden"),
            "repository_read_forbidden",
        )
        self.assertEqual(
            _failure_code(rejected, "repository_write_forbidden"),
            "repository_write_forbidden",
        )

    def test_repository_profile_contracts_are_consistent(self) -> None:
        profiles_root = self.repository / "data" / "profiles"
        actual_profiles = {
            path.name for path in profiles_root.iterdir() if path.is_dir()
        }
        self.assertEqual(actual_profiles, set(ALL_PROFILES))
        required = {
            "HERMES_PROFILE",
            "GITLAB_HOST",
            "GITLAB_ALLOWED_GROUPS",
            "GITLAB_TOKEN",
        }
        for profile in sorted(ALL_PROFILES):
            with self.subTest(profile=profile):
                profile_root = profiles_root / profile
                profile_config = yaml.safe_load(
                    (profile_root / "config.yaml").read_text(encoding="utf-8")
                )
                passthrough = set(
                    profile_config["terminal"]["env_passthrough"]
                )
                self.assertTrue(required.issubset(passthrough))
                env_example = read_profile_env(
                    profile_root / ".env.example"
                )
                self.assertEqual(env_example["HERMES_PROFILE"], profile)
                self.assertEqual(
                    env_example["GITLAB_HOST"],
                    "https://green-git.hollysys.net",
                )
                git_config = (
                    profile_root / "home" / ".gitconfig"
                ).read_text(encoding="utf-8")
                self.assertIn("username = oauth2", git_config)
                self.assertIn(
                    "helper = !/usr/local/bin/gitlab-credential",
                    git_config,
                )
                self.assertNotIn("GITLAB_TOKEN", git_config)
                self.assertNotIn("insteadOf", git_config)


if __name__ == "__main__":
    unittest.main()
