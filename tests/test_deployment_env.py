import base64
import importlib.util
import os
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
VALIDATOR_PATH = ROOT / "scripts" / "validate-deployment-env.py"
SPEC = importlib.util.spec_from_file_location("validate_deployment_env", VALIDATOR_PATH)
VALIDATOR = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(VALIDATOR)


def current_revision() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def valid_env_text(revision: str) -> str:
    salt = base64.b64encode(b"s" * 16).decode()
    digest = base64.b64encode(b"d" * 32).decode()
    secret = base64.b64encode(b"k" * 32).decode()
    return "\n".join(
        [
            f"HERMES_IMAGE={VALIDATOR.EXPECTED_IMAGE}",
            f"FLEET_BUNDLE_REF={revision}",
            "HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin",
            f"HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH='scrypt$16384$8$1${salt}${digest}'",
            f"HERMES_DASHBOARD_BASIC_AUTH_SECRET='{secret}'",
            "FLEET_FORCE_CONFIG=0",
        ]
    ) + "\n"


class DeploymentEnvValidationTests(unittest.TestCase):
    def write_env(self, root: pathlib.Path, text: str) -> pathlib.Path:
        path = root / ".env"
        path.write_text(text, encoding="utf-8")
        path.chmod(0o600)
        return path

    def test_valid_hash_secret_permissions_and_repository_ref_pass(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = self.write_env(pathlib.Path(temp_dir), valid_env_text(current_revision()))
            values = VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            VALIDATOR.validate_repository_lock(ROOT, values["FLEET_BUNDLE_REF"])
            self.assertEqual("admin", values["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"])

    def test_plaintext_password_and_floating_image_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            text = valid_env_text(current_revision())
            text += "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=forbidden\n"
            text = text.replace(VALIDATOR.EXPECTED_IMAGE, "nousresearch/hermes-agent:latest")
            env_path = self.write_env(pathlib.Path(temp_dir), text)
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            self.assertIn("plaintext dashboard password", str(caught.exception))

    def test_malformed_hash_and_short_signing_secret_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            text = valid_env_text(current_revision())
            text = text.replace("scrypt$16384$8$1$", "scrypt$2$1$1$")
            text = text.replace(
                base64.b64encode(b"k" * 32).decode(),
                base64.b64encode(b"short").decode(),
            )
            env_path = self.write_env(pathlib.Path(temp_dir), text)
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            self.assertIn("scrypt parameters", str(caught.exception))

    def test_password_hash_must_be_single_quoted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            text = valid_env_text(current_revision()).replace(
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH='",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH=",
            ).replace("'\nHERMES_DASHBOARD_BASIC_AUTH_SECRET", "\nHERMES_DASHBOARD_BASIC_AUTH_SECRET", 1)
            env_path = self.write_env(pathlib.Path(temp_dir), text)
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            self.assertIn("must be single-quoted", str(caught.exception))

    def test_force_config_must_be_restored_for_acceptance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = self.write_env(
                pathlib.Path(temp_dir),
                valid_env_text(current_revision()).replace("FLEET_FORCE_CONFIG=0", "FLEET_FORCE_CONFIG=1"),
            )
            VALIDATOR.validate_deployment_env(
                env_path,
                expected_uid=os.getuid(),
                allow_force_config=True,
            )
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            self.assertIn("restored to 0", str(caught.exception))

    def test_wrong_mode_symlink_and_head_drift_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            env_path = self.write_env(root, valid_env_text("b" * 40))
            env_path.chmod(0o640)
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            self.assertIn("mode must be 0600", str(caught.exception))
            env_path.chmod(0o600)
            values = VALIDATOR.validate_deployment_env(env_path, expected_uid=os.getuid())
            with self.assertRaises(VALIDATOR.DeploymentEnvError) as caught:
                VALIDATOR.validate_repository_lock(ROOT, values["FLEET_BUNDLE_REF"])
            self.assertIn("does not match", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
