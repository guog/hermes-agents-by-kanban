from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parent.parent
        cls.compose = yaml.safe_load(
            (root / "docker-compose.yaml").read_text(encoding="utf-8")
        )

    def test_controller_is_container_main_process(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["command"], ["python", "-m", "hollysys_controller.daemon"]
        )
        self.assertEqual(service["restart"], "unless-stopped")

    def test_dashboard_is_localhost_only(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertTrue(
            all(str(port).startswith("127.0.0.1:") for port in service["ports"])
        )

    def test_image_is_digest_pinned_for_linux_amd64(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(service["platform"], "linux/amd64")
        self.assertIn("@sha256:", service["image"])


if __name__ == "__main__":
    unittest.main()
