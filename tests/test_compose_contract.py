from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class ComposeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parent.parent
        cls.root = root
        cls.compose = yaml.safe_load(
            (root / "docker-compose.yaml").read_text(encoding="utf-8")
        )

    def test_controller_is_container_main_process(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["command"],
            ["python", "-m", "hollysys_controller.daemon"],
        )
        self.assertEqual(service["restart"], "unless-stopped")

    def test_dashboard_is_published_on_configured_port(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["ports"],
            ["${HERMES_DASHBOARD_PORT:-9119}:9119"],
        )

    def test_image_uses_selected_release_tag_for_linux_amd64(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(service["platform"], "linux/amd64")
        self.assertEqual(
            service["image"],
            "${HERMES_IMAGE:-nousresearch/hermes-agent:v2026.7.20}",
        )
        self.assertEqual(service["pull_policy"], "missing")

    def test_hollysysctl_uses_the_managed_python_environment(self) -> None:
        wrapper = (self.root / "hollysysctl").read_text(encoding="utf-8")
        self.assertIn(
            "exec /opt/hermes/.venv/bin/python -m hollysys_controller.cli",
            wrapper,
        )
        self.assertNotIn("exec python -m hollysys_controller.cli", wrapper)

    def test_write_tools_allow_runtime_data_and_managed_worktrees(self) -> None:
        environment = self.compose["services"]["hermes"]["environment"]
        self.assertEqual(
            environment["HERMES_WRITE_SAFE_ROOT"],
            "/opt/data:/workspace/projects",
        )

if __name__ == "__main__":
    unittest.main()
