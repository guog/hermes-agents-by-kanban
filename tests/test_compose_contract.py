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
        self.assertTrue(controller["init"])
        self.assertNotIn(
            "init",
            hermes,
            "Hermes uses s6-overlay /init, which must remain PID 1",
        )
        self.assertEqual(
            controller["entrypoint"],
            [
                "/bin/sh",
                "/opt/fleet/container/run-hollysys-controller.sh",
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
        self.assertIn("./skills:/opt/skills:ro", controller["volumes"])
        self.assertIn(
            "controller-socket:/run/hollysys-controller",
            hermes["volumes"],
        )
        self.assertIn(
            "./controller/config.yaml:/opt/hollysys-controller/config.yaml:ro",
            hermes["volumes"],
        )
        entrypoint = (
            self.root / "container" / "run-hollysys-controller.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('usermod -u "$target_uid" hermes', entrypoint)
        self.assertIn('groupmod -o -g "$target_gid" hermes', entrypoint)
        self.assertIn(
            "sync-lark-config.py",
            entrypoint,
        )
        self.assertIn(
            "exec setpriv --reuid=hermes --regid=hermes --init-groups",
            entrypoint,
        )
        self.assertIn("/workspace/projects", entrypoint)

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
        self.assertIn("TIRITH_VERSION=0.3.3", dockerfile)
        self.assertIn(
            "TIRITH_SHA256="
            "6cdbe35e8f9ccf42e70ad95b501c93cd218ac18201c3df958d54f6ba0d995ce2",
            dockerfile,
        )
        self.assertIn("/usr/local/bin/tirith", dockerfile)
        self.assertIn("jq", dockerfile)
        self.assertIn("util-linux", dockerfile)
        self.assertIn("patch-hermes-terminal.py", dockerfile)
        self.assertIn("sync-lark-config.py", dockerfile)
        self.assertIn(
            "chmod -R a+rX /opt/hollysys-controller-src",
            dockerfile,
        )

    def test_feishu_adapter_dependencies_are_pinned_and_verified(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertIn(
            "./container/ensure-feishu.sh:/etc/cont-init.d/02-hollysys-feishu:ro",
            service["volumes"],
        )
        image_requirements = (
            self.root / "requirements-controller.txt"
        ).read_text(encoding="utf-8")
        script = (self.root / "container/ensure-feishu.sh").read_text(
            encoding="utf-8"
        )
        for requirement in (
            "lark-oapi==1.6.8",
            "qrcode==7.4.2",
            "requests-toolbelt==1.0.0",
        ):
            self.assertIn(requirement, image_requirements)
            self.assertIn(requirement, script)
        self.assertIn("/opt/data/.cache/uv", script)
        self.assertIn("import lark_oapi", script)
        self.assertEqual(
            (self.root / "container/mirrors/uv.toml")
            .read_text(encoding="utf-8")
            .strip(),
            'index-url = "https://mirrors.aliyun.com/pypi/simple/"',
        )

    def test_required_feishu_gateways_are_declared_running(self) -> None:
        for profile in ("dispatcher", "fde", "prd-writer"):
            with self.subTest(profile=profile):
                state_path = (
                    self.root
                    / "data"
                    / "profiles"
                    / profile
                    / "gateway_state.json"
                )
                self.assertTrue(state_path.is_file())
                self.assertTrue(
                    state_path.is_relative_to(self.root / "data" / "profiles")
                )
                state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["desired_state"], "running")
                self.assertEqual(state["gateway_state"], "running")

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

    def test_default_network_avoids_company_172_routes(self) -> None:
        subnet = self.compose["networks"]["default"]["ipam"]["config"][0][
            "subnet"
        ]
        self.assertEqual(
            subnet,
            "${HOLLYSYS_DOCKER_SUBNET:-10.253.252.0/29}",
        )

    def test_all_agent_profiles_share_runtime_budget_contract(self) -> None:
        profile_configs = sorted(
            (self.root / "data" / "profiles").glob("*/config.yaml")
        )
        self.assertEqual(len(profile_configs), 12)
        for config_path in profile_configs:
            with self.subTest(profile=config_path.parent.name):
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    config["agent"]["reasoning_effort"],
                    "xhigh",
                )
                self.assertEqual(config["agent"]["max_turns"], 500)
                self.assertEqual(config["agent"]["api_max_retries"], 10)

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
