import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-profile-envs.py"
SPEC = importlib.util.spec_from_file_location("validate_profile_envs", VALIDATOR_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VALIDATOR)


def env_text(profile: str) -> str:
    common = [
        f"HERMES_PROFILE={profile}",
        "GITLAB_HOST=green-git.internal",
        "GITLAB_ALLOWED_GROUPS=group,group/subgroup",
        f"GITLAB_TOKEN='git-$#={profile}'",
    ]
    if profile in VALIDATOR.GATEWAY_PROFILES:
        ports = {"dispatcher": 8642, "prd-writer": 8643, "fde": 8644}
        common.extend(
            [
                f"API_SERVER_PORT={ports[profile]}",
                f"FEISHU_APP_ID=cli_{profile}",
                f"FEISHU_APP_SECRET='feishu-$#={profile}'",
                "FEISHU_DOMAIN=feishu",
                "FEISHU_CONNECTION_MODE=websocket",
                "FEISHU_ALLOWED_USERS=ou_user",
                "FEISHU_HOME_CHANNEL=oc_channel",
                "FEISHU_GROUP_POLICY=allowlist",
                "FEISHU_REQUIRE_MENTION=true",
                f"LARKSUITE_CLI_CONFIG_DIR=/opt/data/profiles/{profile}/.lark-cli/config",
                f"LARKSUITE_CLI_DATA_DIR=/opt/data/profiles/{profile}/.lark-cli/data",
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER=1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER=1",
            ]
        )
    return "\n".join(common) + "\n"


def make_valid_tree(root: pathlib.Path) -> None:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    for profile in VALIDATOR.PROFILES:
        profile_dir = root / profile
        profile_dir.mkdir(mode=0o700)
        profile_dir.chmod(0o700)
        env_path = profile_dir / ".env"
        env_path.write_text(env_text(profile), encoding="utf-8")
        env_path.chmod(0o600)


class ProfileEnvInitializationTests(unittest.TestCase):
    def test_initializer_is_idempotent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = pathlib.Path(temp_dir) / "custom-hermes"
            env = dict(os.environ, HERMES_DATA_DIR=str(data_dir))
            first = subprocess.run(
                [str(ROOT / "scripts" / "init-profile-envs.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertIn("created 12, preserved 0", first.stdout)

            dispatcher_env = data_dir / "profiles" / "dispatcher" / ".env"
            marker = "GITLAB_TOKEN='do-not-overwrite-$#='"
            dispatcher_env.write_text(env_text("dispatcher") + marker + "\n", encoding="utf-8")
            dispatcher_env.chmod(0o644)

            second = subprocess.run(
                [str(ROOT / "scripts" / "init-profile-envs.sh")],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertIn("created 0, preserved 12", second.stdout)
            self.assertIn(marker, dispatcher_env.read_text(encoding="utf-8"))
            self.assertEqual(0o600, dispatcher_env.stat().st_mode & 0o777)
            self.assertEqual(0o700, dispatcher_env.parent.stat().st_mode & 0o777)


class ProfileEnvValidationTests(unittest.TestCase):
    def test_valid_profiles_support_quoted_special_characters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            values = VALIDATOR.validate_profiles(profiles_root, os.getuid())
            self.assertEqual("git-$#=coder", values["coder"]["GITLAB_TOKEN"])
            self.assertEqual("feishu-$#=fde", values["fde"]["FEISHU_APP_SECRET"])

    def test_duplicate_secret_fails_without_leaking_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            duplicate = "private-duplicate-$#="
            for profile in ("coder", "tester"):
                env_path = profiles_root / profile / ".env"
                env_path.write_text(
                    env_text(profile).replace(f"'git-$#={profile}'", f"'{duplicate}'"),
                    encoding="utf-8",
                )
                env_path.chmod(0o600)
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_profiles(profiles_root, os.getuid())
            self.assertIn("GITLAB_TOKEN must be unique", str(caught.exception))
            self.assertNotIn(duplicate, str(caught.exception))

    def test_worker_feishu_variable_and_missing_token_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            env_path = profiles_root / "coder" / ".env"
            env_path.write_text(
                env_text("coder").replace("GITLAB_TOKEN='git-$#=coder'", "GITLAB_TOKEN=")
                + "FEISHU_APP_ID=forbidden\n",
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_profiles(profiles_root, os.getuid())
            message = str(caught.exception)
            self.assertIn("required variable GITLAB_TOKEN is empty", message)
            self.assertIn("Feishu/Lark variables are not allowed", message)

    def test_duplicate_feishu_identity_and_gateway_port_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            env_path = profiles_root / "fde" / ".env"
            env_path.write_text(
                env_text("fde")
                .replace("API_SERVER_PORT=8644", "API_SERVER_PORT=8642")
                .replace("FEISHU_APP_ID=cli_fde", "FEISHU_APP_ID=cli_dispatcher")
                .replace("'feishu-$#=fde'", "'feishu-$#=dispatcher'"),
                encoding="utf-8",
            )
            env_path.chmod(0o600)
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_profiles(profiles_root, os.getuid())
            message = str(caught.exception)
            self.assertIn("API_SERVER_PORT must be unique", message)
            self.assertIn("FEISHU_APP_ID must be unique", message)
            self.assertIn("FEISHU_APP_SECRET must be unique", message)

    def test_wrong_owner_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_profiles(profiles_root, os.getuid() + 1)
            self.assertIn("must be owned by the Hermes runtime user", str(caught.exception))

    def test_wrong_mode_and_symbolic_link_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profiles_root = pathlib.Path(temp_dir) / "profiles"
            make_valid_tree(profiles_root)
            (profiles_root / "planner" / ".env").chmod(0o640)
            target = profiles_root / "coder" / ".env"
            target.unlink()
            target.symlink_to(profiles_root / "tester" / ".env")
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_profiles(profiles_root, os.getuid())
            message = str(caught.exception)
            self.assertIn("planner: .env mode must be 0600", message)
            self.assertIn("coder: .env must be a regular file, not a symbolic link", message)


if __name__ == "__main__":
    unittest.main()
