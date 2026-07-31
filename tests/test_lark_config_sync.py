from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class LarkConfigSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.profiles = self.root / "profiles"
        self.script = (
            Path(__file__).resolve().parents[1]
            / "container"
            / "sync-lark-config.py"
        )
        for profile in ("dispatcher", "fde", "prd-writer"):
            profile_root = self.profiles / profile
            profile_root.mkdir(parents=True)
            env_file = profile_root / ".env"
            env_file.write_text(
                f"FEISHU_APP_ID=app-{profile}\n"
                f"FEISHU_APP_SECRET=secret-{profile}\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_sync(self) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HOLLYSYS_PROFILES_ROOT"] = str(self.profiles)
        return subprocess.run(
            [sys.executable, str(self.script)],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_sync_generates_bot_only_configs_without_printing_secrets(
        self,
    ) -> None:
        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("secret-", result.stdout)
        for profile in ("dispatcher", "fde", "prd-writer"):
            config_path = (
                self.profiles
                / profile
                / ".lark-cli"
                / "config"
                / "hermes"
                / "config.json"
            )
            payload = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                payload,
                {
                    "apps": [
                        {
                            "name": profile,
                            "appId": f"app-{profile}",
                            "appSecret": f"secret-{profile}",
                            "brand": "feishu",
                            "defaultAs": "bot",
                            "strictMode": "bot",
                            "users": [],
                        }
                    ]
                },
            )
            self.assertEqual(
                stat.S_IMODE(config_path.stat().st_mode),
                0o600,
            )

    def test_sync_replaces_rotated_credentials_atomically(self) -> None:
        self.assertEqual(self.run_sync().returncode, 0)
        dispatcher_env = self.profiles / "dispatcher" / ".env"
        dispatcher_env.write_text(
            "FEISHU_APP_ID=rotated-app\n"
            "FEISHU_APP_SECRET=rotated-secret\n",
            encoding="utf-8",
        )
        dispatcher_env.chmod(0o600)

        result = self.run_sync()

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(
            (
                self.profiles
                / "dispatcher"
                / ".lark-cli"
                / "config"
                / "hermes"
                / "config.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(payload["apps"][0]["appId"], "rotated-app")
        self.assertEqual(payload["apps"][0]["appSecret"], "rotated-secret")
        self.assertNotIn("rotated-secret", result.stdout)


if __name__ == "__main__":
    unittest.main()
