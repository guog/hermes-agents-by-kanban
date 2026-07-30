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

    def test_controller_is_an_independent_compose_service(self) -> None:
        controller = self.compose["services"]["controller"]
        hermes = self.compose["services"]["hermes"]
        self.assertEqual(controller["restart"], "unless-stopped")
        self.assertEqual(
            controller["entrypoint"],
            [
                "/opt/hermes/.venv/bin/python",
                "-m",
                "hollysys_controller.daemon",
            ],
        )
        self.assertNotIn("secrets", hermes)
        self.assertEqual(
            controller["secrets"][0]["source"],
            "hollysys_controller_gitlab_token",
        )
        self.assertEqual(
            hermes["depends_on"]["controller"]["condition"],
            "service_healthy",
        )
        self.assertIn(
            "controller-socket:/run/hollysys-controller",
            controller["volumes"],
        )
        self.assertIn(
            "controller-socket:/run/hollysys-controller",
            hermes["volumes"],
        )

    def test_container_health_uses_local_liveness_probe(self) -> None:
        healthcheck = self.compose["services"]["hermes"]["healthcheck"]
        self.assertEqual(
            healthcheck["test"][1],
            "/usr/local/bin/hollysysctl",
        )
        self.assertEqual(healthcheck["test"][-2:], ["--probe", "liveness"])

    def test_derived_image_pins_base_and_toolchain(self) -> None:
        dockerfile = (self.root / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "v2026.7.20@sha256:f7b35053268f532f98955195c909f15a"
            "230470fbcbdacaa9fdecb95707dad04a",
            dockerfile,
        )
        self.assertIn("NODE_VERSION=22.18.0", dockerfile)
        self.assertIn("DOTNET_SDK_VERSION=8.0.423", dockerfile)
        self.assertIn("jq", dockerfile)
        self.assertIn("patch-hermes-terminal.py", dockerfile)

    def test_feishu_adapter_dependencies_are_pinned_and_verified(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertIn(
            "./container/ensure-feishu.sh:/etc/cont-init.d/02-hollysys-feishu:ro",
            service["volumes"],
        )
        script = (self.root / "container/ensure-feishu.sh").read_text(
            encoding="utf-8"
        )
        for requirement in (
            "lark-oapi==1.6.8",
            "qrcode==7.4.2",
            "requests-toolbelt==1.0.0",
        ):
            self.assertIn(requirement, script)
        self.assertIn("/opt/data/.cache/uv", script)
        self.assertIn("import lark_oapi", script)
        self.assertEqual(
            (self.root / "container/mirrors/uv.toml")
            .read_text(encoding="utf-8")
            .strip(),
            'index-url = "https://mirrors.aliyun.com/pypi/simple/"',
        )

    def test_dashboard_is_published_on_configured_port(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["ports"],
            ["${HERMES_DASHBOARD_HOST_PORT:-9119}:9119"],
        )
        self.assertEqual(service["environment"]["HERMES_DASHBOARD_PORT"], "9119")

    def test_container_name_is_parameterized_for_colocated_deployments(
        self,
    ) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(
            service["container_name"],
            "${HERMES_CONTAINER_NAME:-hermes}",
        )

    def test_image_uses_derived_v4_image_for_linux_amd64(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(service["platform"], "linux/amd64")
        self.assertEqual(
            service["image"],
            "${HOLLYSYS_IMAGE:-hollysys/hermes-agent:v4}",
        )
        self.assertEqual(
            service["build"]["args"]["HERMES_BASE_IMAGE"],
            "${HERMES_BASE_IMAGE:-nousresearch/hermes-agent:v2026.7.20@"
            "sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fde"
            "cb95707dad04a}",
        )

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
        self.assertEqual(
            environment["HERMES_SCRATCH_DIR"],
            "/opt/data/scratch",
        )

    def test_locked_clis_are_installed_for_login_shells(self) -> None:
        install_script = (
            self.root / "container/install-git-wrapper.sh"
        ).read_text(encoding="utf-8")
        for executable in (
            "git",
            "gitlab-askpass",
            "gitlab-credential",
            "glab",
            "lark-cli",
        ):
            with self.subTest(executable=executable):
                self.assertIn(executable, install_script)
        self.assertIn("/usr/local/bin", install_script)
        self.assertIn("-o root", install_script)
        self.assertIn("-g root", install_script)
        self.assertIn("-m 0555", install_script)

if __name__ == "__main__":
    unittest.main()
