from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.helpers import config


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_controller_token_requires_private_regular_file(self) -> None:
        self.config.token_file.write_text("secret\n", encoding="utf-8")
        self.config.token_file.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.config.read_token()

        self.config.token_file.chmod(0o600)
        self.assertEqual(self.config.read_token(), "secret")

    def test_controller_token_rejects_symlink(self) -> None:
        target = self.root / "actual-token"
        target.write_text("secret\n", encoding="utf-8")
        target.chmod(0o600)
        self.config.token_file.symlink_to(target)
        with self.assertRaisesRegex(PermissionError, "symlink"):
            self.config.read_token()


if __name__ == "__main__":
    unittest.main()
