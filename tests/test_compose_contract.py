from __future__ import annotations

import os
import subprocess
import tempfile
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

    def test_dashboard_is_localhost_only(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertTrue(
            all(str(port).startswith("127.0.0.1:") for port in service["ports"])
        )

    def test_image_is_digest_pinned_for_linux_amd64(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertEqual(service["platform"], "linux/amd64")
        self.assertIn("@sha256:", service["image"])

    def test_package_managers_use_mounted_alibaba_mirrors(self) -> None:
        service = self.compose["services"]["hermes"]
        volumes = set(service["volumes"])
        expected_mounts = {
            "./container/mirrors/debian.sources:/etc/apt/sources.list.d/debian.sources:ro",
            "./container/mirrors/sources.list:/etc/apt/sources.list:ro",
            "./container/mirrors/pip.conf:/etc/pip.conf:ro",
            "./container/mirrors/uv.toml:/etc/uv/uv.toml:ro",
        }
        self.assertTrue(expected_mounts.issubset(volumes))

        environment = service["environment"]
        self.assertEqual(
            environment["PIP_INDEX_URL"],
            "https://mirrors.aliyun.com/pypi/simple/",
        )
        self.assertEqual(
            environment["UV_DEFAULT_INDEX"],
            "https://mirrors.aliyun.com/pypi/simple/",
        )
        self.assertEqual(
            environment["NPM_CONFIG_REGISTRY"],
            "https://registry.npmmirror.com/",
        )
        self.assertEqual(
            environment["NPM_CONFIG_USERCONFIG"],
            "/opt/fleet/container/mirrors/npmrc",
        )
        self.assertEqual(
            environment["COREPACK_NPM_REGISTRY"],
            "https://registry.npmmirror.com",
        )
        self.assertEqual(environment["PIP_EXTRA_INDEX_URL"], "")
        self.assertEqual(environment["UV_EXTRA_INDEX_URL"], "")

        npmrc = (
            self.root / "container/mirrors/npmrc"
        ).read_text(encoding="utf-8")
        self.assertIn("replace-registry-host=always", npmrc)

        debian_sources = (
            self.root / "container/mirrors/debian.sources"
        ).read_text(encoding="utf-8")
        self.assertIn("https://mirrors.aliyun.com/debian", debian_sources)
        self.assertNotIn("deb.debian.org", debian_sources)
        self.assertNotIn("security.debian.org", debian_sources)

    def test_dotnet8_installer_is_startup_idempotent(self) -> None:
        service = self.compose["services"]["hermes"]
        self.assertIn(
            "./container:/opt/fleet/container:ro",
            service["volumes"],
        )
        self.assertIn(
            "./container/ensure-dotnet8.sh:/etc/cont-init.d/00-hollysys-dotnet8:ro",
            service["volumes"],
        )

        installer_path = self.root / "container/ensure-dotnet8.sh"
        self.assertTrue(os.access(installer_path, os.X_OK))
        installer = installer_path.read_text(encoding="utf-8")
        self.assertIn("dotnet --list-sdks", installer)
        self.assertIn(".NET SDK 8 already exists", installer)
        self.assertIn(
            "https://packages.microsoft.com/config/debian/13/packages-microsoft-prod.deb",
            installer,
        )
        self.assertIn(
            "apt-get install -y --no-install-recommends dotnet-sdk-8.0",
            installer,
        )
        self.assertIn("dpkg --purge packages-microsoft-prod", installer)

    def test_dotnet8_installer_skips_when_sdk8_is_available(self) -> None:
        installer = self.root / "container/ensure-dotnet8.sh"
        with tempfile.TemporaryDirectory() as temp_dir:
            fake_dotnet = Path(temp_dir) / "dotnet"
            fake_dotnet.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"--list-sdks\" ]; then\n"
                "  printf '%s\\n' '8.0.999 [/tmp/fake-sdk]'\n"
                "fi\n",
                encoding="utf-8",
            )
            fake_dotnet.chmod(0o755)
            environment = dict(os.environ)
            environment["PATH"] = f"{temp_dir}:/usr/bin:/bin"
            completed = subprocess.run(
                ["/bin/sh", str(installer)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("installation skipped", completed.stdout)
        self.assertNotIn("installing it for Debian 13", completed.stdout)


if __name__ == "__main__":
    unittest.main()
