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
            "./container/services.d/hollysys-controller:/etc/services.d/hollysys-controller:ro",
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
        self.assertIn(
            "./container/install-git-wrapper.sh:/etc/cont-init.d/01-hollysys-git-wrapper:ro",
            service["volumes"],
        )
        self.assertTrue(service["environment"]["PATH"].startswith(
            "/run/hollysys/bin:"
        ))
        install_script = (
            self.root / "container/install-git-wrapper.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("-o root", install_script)
        self.assertIn("-g root", install_script)
        self.assertIn("-m 0555", install_script)
        self.assertIn('"/usr/local/bin/$executable"', install_script)

        finish_script = (
            self.root / "container/services.d/hollysys-controller/finish"
        ).read_text(encoding="utf-8")
        self.assertIn("HOLLYSYS_FATAL_RESTART_BACKOFF_SECONDS", finish_script)

    def test_container_health_uses_local_liveness_probe(self) -> None:
        healthcheck = self.compose["services"]["hermes"]["healthcheck"]
        self.assertEqual(
            healthcheck["test"][1],
            "/opt/hermes/.venv/bin/python",
        )
        self.assertEqual(healthcheck["test"][-2:], ["--probe", "liveness"])

    def test_dotnet_sdk_is_reused_from_persistent_runtime_data(self) -> None:
        script = (
            self.root / "container/ensure-dotnet8.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "PERSISTENT_DOTNET_ROOT=/opt/data/.dotnet",
            script,
        )
        self.assertIn(
            "mktemp -d /opt/data/.dotnet-stage.XXXXXX",
            script,
        )
        self.assertIn('mv "$persistent_stage" "$PERSISTENT_DOTNET_ROOT"', script)
        self.assertIn(
            "persistent .NET root exists but does not contain a valid SDK 8",
            script,
        )

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
