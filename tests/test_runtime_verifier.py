import importlib.util
import os
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-profile-envs.py"
SPEC = importlib.util.spec_from_file_location("runtime_validate_profile_envs", VALIDATOR_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VALIDATOR)


def profile_env(profile: str) -> str:
    lines = [
        f"HERMES_PROFILE={profile}",
        "GITLAB_HOST=green-git.internal",
        "GITLAB_ALLOWED_GROUPS=group",
        f"GITLAB_TOKEN='secret-$#={profile}'",
    ]
    if profile in VALIDATOR.GATEWAY_PROFILES:
        ports = {"dispatcher": 8642, "prd-writer": 8643, "fde": 8644}
        lines.extend(
            [
                f"API_SERVER_PORT={ports[profile]}",
                f"FEISHU_APP_ID=cli_{profile}",
                f"FEISHU_APP_SECRET='app-secret-$#={profile}'",
                "FEISHU_DOMAIN=feishu",
                "FEISHU_CONNECTION_MODE=websocket",
                "FEISHU_ALLOWED_USERS=ou_user",
                "FEISHU_HOME_CHANNEL=oc_channel",
                "FEISHU_GROUP_POLICY=allowlist",
                "FEISHU_REQUIRE_MENTION=true",
                f"LARKSUITE_CLI_CONFIG_DIR=/opt/data/profiles/{profile}/.lark-cli/config",
                f"LARKSUITE_CLI_DATA_DIR=/opt/data/profiles/{profile}/.lark-cli/data",
            ]
        )
    return "\n".join(lines) + "\n"


def make_runtime_tree(root: pathlib.Path) -> None:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    for profile in VALIDATOR.PROFILES:
        profile_dir = root / profile
        profile_dir.mkdir(mode=0o700)
        profile_dir.chmod(0o700)
        (profile_dir / "home").mkdir(mode=0o700)
        env_path = profile_dir / ".env"
        env_path.write_text(profile_env(profile), encoding="utf-8")
        env_path.chmod(0o600)
        if profile in VALIDATOR.GATEWAY_PROFILES:
            for directory in (
                profile_dir / ".lark-cli",
                profile_dir / ".lark-cli" / "config",
                profile_dir / ".lark-cli" / "data",
            ):
                directory.mkdir(mode=0o700)
                directory.chmod(0o700)
            state = profile_dir / ".lark-cli" / "config" / "binding.enc"
            state.write_text("encrypted-state", encoding="utf-8")
            state.chmod(0o600)


def write_fake_glab(path: pathlib.Path) -> None:
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import os
            import sys

            profiles = {VALIDATOR.PROFILES!r}
            roles = {VALIDATOR.EXPECTED_ACCESS_LEVELS!r}
            profile = os.environ["HERMES_PROFILE"]
            endpoint = sys.argv[2]
            user_id = 1000 + profiles.index(profile)
            if os.environ.get("FAKE_DUPLICATE_IDENTITY") == profile:
                user_id = 1000
            if os.environ.get("FAKE_GLAB_SECRET_ERROR") == profile:
                sys.stderr.write(os.environ["GITLAB_TOKEN"])
                raise SystemExit(1)
            if endpoint == "user":
                print(json.dumps({{"id": user_id, "username": "bot-" + profile}}))
            elif "/members/all/" in endpoint:
                level = roles[profile]
                if os.environ.get("FAKE_LOW_ROLE") == profile:
                    level = 10
                print(json.dumps({{"access_level": level}}))
            elif endpoint.startswith("groups/"):
                print(json.dumps({{"id": 77}}))
            else:
                raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    path.chmod(0o755)


class RuntimeIdentityTests(unittest.TestCase):
    def test_distinct_gitlab_identities_and_minimum_roles_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir) / "profiles"
            make_runtime_tree(root)
            glab = pathlib.Path(temp_dir) / "glab"
            write_fake_glab(glab)
            values = VALIDATOR.validate_profiles(root, os.getuid())
            identities = VALIDATOR.validate_runtime_identities(
                values, root, os.getuid(), glab_bin=str(glab)
            )
            self.assertEqual(12, len(identities))
            self.assertEqual(12, len({item[1] for item in identities}))

    def test_duplicate_identity_and_insufficient_role_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir) / "profiles"
            make_runtime_tree(root)
            glab = pathlib.Path(temp_dir) / "glab"
            write_fake_glab(glab)
            values = VALIDATOR.validate_profiles(root, os.getuid())
            with mock.patch.dict(
                os.environ,
                {"FAKE_DUPLICATE_IDENTITY": "coder", "FAKE_LOW_ROLE": "dispatcher"},
            ):
                with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                    VALIDATOR.validate_runtime_identities(
                        values, root, os.getuid(), glab_bin=str(glab)
                    )
            message = str(caught.exception)
            self.assertIn("GitLab identity must be unique", message)
            self.assertIn("below required 40", message)

    def test_glab_failure_does_not_leak_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir) / "profiles"
            make_runtime_tree(root)
            glab = pathlib.Path(temp_dir) / "glab"
            write_fake_glab(glab)
            values = VALIDATOR.validate_profiles(root, os.getuid())
            secret = values["tester"]["GITLAB_TOKEN"]
            with mock.patch.dict(os.environ, {"FAKE_GLAB_SECRET_ERROR": "tester"}):
                with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                    VALIDATOR.validate_runtime_identities(
                        values, root, os.getuid(), glab_bin=str(glab)
                    )
            self.assertNotIn(secret, str(caught.exception))

    def test_worker_lark_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir) / "profiles"
            make_runtime_tree(root)
            (root / "coder" / ".lark-cli").mkdir(mode=0o700)
            values = VALIDATOR.validate_profiles(root, os.getuid())
            with self.assertRaises(VALIDATOR.ProfileEnvError) as caught:
                VALIDATOR.validate_runtime_identities(
                    values, root, os.getuid(), glab_bin="unused"
                )
            self.assertIn("worker profile must not have lark-cli state", str(caught.exception))


class RuntimeShellTests(unittest.TestCase):
    def run_verifier(self, *, running: bool = True, container_error: str = "") -> subprocess.CompletedProcess[str]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        bundle = pathlib.Path(temp.name) / "bundle"
        scripts = bundle / "scripts"
        fake_bin = pathlib.Path(temp.name) / "bin"
        scripts.mkdir(parents=True)
        fake_bin.mkdir()
        shutil.copy2(ROOT / "scripts" / "verify-runtime.sh", scripts / "verify-runtime.sh")
        verify_bundle = scripts / "verify-bundle.sh"
        verify_bundle.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        verify_bundle.chmod(0o755)
        uname = fake_bin / "uname"
        uname.write_text(
            "#!/usr/bin/env bash\n[[ ${1:-} == -s ]] && echo Linux || echo x86_64\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)
        docker = fake_bin / "docker"
        docker.write_text(
            textwrap.dedent(
                r"""
                #!/usr/bin/env bash
                set -euo pipefail
                command_line="$*"
                case "${command_line}" in
                  "compose version") exit 0 ;;
                  "compose config --images")
                    printf '%s\n%s\n' 'nousresearch/hermes-agent:v2026.7.20' 'nousresearch/hermes-agent:v2026.7.20'
                    ;;
                  image\ inspect*) exit 0 ;;
                  "compose ps --status running --services")
                    if [[ "${FAKE_RUNNING:-1}" == 1 ]]; then
                      echo hermes
                    fi
                    ;;
                  compose\ exec*)
                    if [[ -n "${FAKE_CONTAINER_ERROR:-}" ]]; then
                      echo "${FAKE_CONTAINER_ERROR}" >&2
                      exit 1
                    fi
                    exit 0
                    ;;
                  *) echo "unexpected fake docker call: ${command_line}" >&2; exit 2 ;;
                esac
                """
            ).lstrip(),
            encoding="utf-8",
        )
        docker.chmod(0o755)
        env = dict(
            os.environ,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_RUNNING="1" if running else "0",
            FAKE_CONTAINER_ERROR=container_error,
        )
        return subprocess.run(
            [str(scripts / "verify-runtime.sh")],
            cwd=bundle,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_container_not_running_is_reported(self):
        result = self.run_verifier(running=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("hermes Compose service is not running", result.stderr)

    def test_container_version_profile_and_gateway_failures_are_reported(self):
        for message in (
            "expected Hermes 0.19.0, found 0.20.0",
            "profile is missing: coder",
            "Gateway is not running: dispatcher",
        ):
            with self.subTest(message=message):
                result = self.run_verifier(container_error=message)
                self.assertNotEqual(0, result.returncode)
                self.assertIn(message, result.stderr)

    def test_successful_read_only_shell_check(self):
        result = self.run_verifier()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("deployable read-only runtime checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
