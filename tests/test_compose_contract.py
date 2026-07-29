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

    def test_controller_is_independently_supervised_by_s6(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["command"],
            ["sleep", "infinity"],
        )
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertIn(
            "./container/services.d/hollysys-controller:/run/service/hollysys-controller:ro",
            service["volumes"],
        )
        run_script = (
            self.root / "container/services.d/hollysys-controller/run"
        ).read_text(encoding="utf-8")
        self.assertIn("s6-setuidgid hermes", run_script)
        self.assertIn(
            "/opt/hermes/.venv/bin/python -m hollysys_controller.daemon",
            run_script,
        )

    def test_container_health_uses_local_liveness_probe(self) -> None:
        healthcheck = self.compose["services"]["hermes"]["healthcheck"]
        self.assertEqual(healthcheck["test"][-2:], ["--probe", "liveness"])

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

if __name__ == "__main__":
    unittest.main()
