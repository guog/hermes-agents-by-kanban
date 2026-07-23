import grp
import importlib.util
import os
import pathlib
import pwd
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER_PATH = ROOT / "scripts" / "install-profile-commands.py"
SPEC = importlib.util.spec_from_file_location("install_profile_commands", INSTALLER_PATH)
INSTALLER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INSTALLER)


class ProfileCommandTests(unittest.TestCase):
    def make_executable(self, path: pathlib.Path, body: str) -> pathlib.Path:
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def install(
        self,
        root: pathlib.Path,
        profiles: tuple[str, ...] = ("coder", "tester"),
    ) -> tuple[list[pathlib.Path], pathlib.Path, pathlib.Path]:
        data_root = root / "data"
        data_root.mkdir(mode=0o700)
        fake_hermes = self.make_executable(
            root / "hermes",
            "#!/bin/sh\nprintf '%s\\n' \"$HOME|$HERMES_HOME|$*\"\n",
        )
        fake_setuidgid = self.make_executable(
            root / "s6-setuidgid",
            "#!/bin/sh\nexit 99\n",
        )
        owner = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        installed = INSTALLER.install_profile_commands(
            profiles,
            output_dir=data_root / ".local" / "bin",
            runtime_user=owner,
            owner=owner,
            group=group,
            base_home=data_root,
            hermes_executable=fake_hermes,
            setuidgid_executable=fake_setuidgid,
        )
        return installed, data_root, fake_hermes

    def test_wrappers_select_profiles_and_preserve_arguments(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            installed, data_root, _ = self.install(pathlib.Path(temp_dir))
            self.assertEqual(["coder", "tester"], [path.name for path in installed])
            result = subprocess.run(
                [str(installed[0]), "chat", "-q", "hello world"],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(
                f"{data_root}|{data_root}|-p coder chat -q hello world\n",
                result.stdout,
            )
            for path in installed:
                status = path.stat()
                self.assertEqual(0o755, status.st_mode & 0o777)
                self.assertEqual(os.getuid(), status.st_uid)
                self.assertEqual(os.getgid(), status.st_gid)
                content = path.read_text(encoding="utf-8")
                self.assertIn("s6-setuidgid", content)
                self.assertIn('if [ "${current_uid}" = 0 ]', content)

    def test_reinstall_repairs_managed_wrapper_and_preserves_other_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            installed, data_root, fake_hermes = self.install(root)
            unrelated = installed[0].parent / "operator-tool"
            unrelated.write_text("keep", encoding="utf-8")
            installed[0].write_text("damaged", encoding="utf-8")
            owner = pwd.getpwuid(os.getuid()).pw_name
            group = grp.getgrgid(os.getgid()).gr_name
            second = INSTALLER.install_profile_commands(
                ("coder", "tester"),
                output_dir=installed[0].parent,
                runtime_user=owner,
                owner=owner,
                group=group,
                base_home=data_root,
                hermes_executable=fake_hermes,
                setuidgid_executable=root / "s6-setuidgid",
            )
            self.assertIn("exec", second[0].read_text(encoding="utf-8"))
            self.assertEqual("keep", unrelated.read_text(encoding="utf-8"))

    def test_symlinked_wrapper_and_invalid_profile_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            installed, data_root, fake_hermes = self.install(root)
            installed[0].unlink()
            installed[0].symlink_to(installed[1])
            owner = pwd.getpwuid(os.getuid()).pw_name
            group = grp.getgrgid(os.getgid()).gr_name
            with self.assertRaises(INSTALLER.ProfileCommandError) as caught:
                INSTALLER.install_profile_commands(
                    ("coder",),
                    output_dir=installed[0].parent,
                    runtime_user=owner,
                    owner=owner,
                    group=group,
                    base_home=data_root,
                    hermes_executable=fake_hermes,
                    setuidgid_executable=root / "s6-setuidgid",
                )
            self.assertIn("regular file", str(caught.exception))

            with self.assertRaises(INSTALLER.ProfileCommandError) as caught:
                INSTALLER.install_profile_commands(
                    ("../coder",),
                    output_dir=installed[0].parent,
                    runtime_user=owner,
                    owner=owner,
                    group=group,
                    base_home=data_root,
                    hermes_executable=fake_hermes,
                    setuidgid_executable=root / "s6-setuidgid",
                )
            self.assertIn("invalid profile name", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
