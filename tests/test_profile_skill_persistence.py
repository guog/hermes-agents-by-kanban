import hashlib
import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INITIALIZER_PATH = ROOT / "scripts" / "initialize-profile-skills.py"
SPEC = importlib.util.spec_from_file_location(
    "initialize_profile_skills", INITIALIZER_PATH
)
INITIALIZER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(INITIALIZER)


def write_role(source: pathlib.Path, name: str = "sdd-test-role", body: str = "seed") -> None:
    role = source / name
    role.mkdir(parents=True)
    (role / "SKILL.md").write_text(
        f"---\nname: {name}\n---\n{body}\n",
        encoding="utf-8",
    )
    support = role / "references"
    support.mkdir()
    (support / "contract.md").write_text("seed support\n", encoding="utf-8")


def digest_tree(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode())
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


class ProfileSkillPersistenceTests(unittest.TestCase):
    def make_paths(
        self, temporary: str
    ) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
        root = pathlib.Path(temporary)
        source = root / "bundle-skills"
        target = root / "profile" / "skills"
        markers = root / ".fleet" / "skills-v1"
        source.mkdir()
        target.mkdir(parents=True)
        markers.mkdir(parents=True)
        return source, target, markers

    def initialize(
        self,
        source: pathlib.Path,
        target: pathlib.Path,
        markers: pathlib.Path,
        *,
        new_profile: bool,
    ) -> str:
        return INITIALIZER.initialize_profile_skills(
            profile="tester",
            source=source,
            target=target,
            marker_root=markers,
            new_profile=new_profile,
        )

    def test_new_profile_is_seeded_once_and_marked(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)

            self.assertEqual(
                "seeded",
                self.initialize(source, target, markers, new_profile=True),
            )
            self.assertTrue((target / "sdd-test-role" / "SKILL.md").is_file())
            payload = json.loads((markers / "tester").read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    "schema_version": 1,
                    "profile": "tester",
                    "role_skill": "sdd-test-role",
                },
                payload,
            )

    def test_new_profile_resumes_after_seed_rename_before_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            write_role(target)
            before = digest_tree(target)

            self.assertEqual(
                "seeded",
                self.initialize(source, target, markers, new_profile=True),
            )
            self.assertEqual(before, digest_tree(target))
            self.assertTrue((markers / "tester").is_file())

    def test_approved_content_and_pending_changes_survive_restarts(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            self.initialize(source, target, markers, new_profile=True)

            role = target / "sdd-test-role"
            (role / "SKILL.md").write_text("approved role edit\n", encoding="utf-8")
            (role / "examples").mkdir()
            (role / "examples" / "approved.md").write_text(
                "approved support\n", encoding="utf-8"
            )
            agent_skill = target / "agent-created"
            agent_skill.mkdir()
            (agent_skill / "SKILL.md").write_text(
                "---\nname: agent-created\n---\nagent\n", encoding="utf-8"
            )
            pending = target.parent / "pending" / "skills" / "request-1"
            pending.mkdir(parents=True)
            (pending / "SKILL.md").write_text("pending edit\n", encoding="utf-8")
            before = digest_tree(target.parent)

            self.assertEqual(
                "preserved",
                self.initialize(source, target, markers, new_profile=False),
            )
            self.assertEqual(
                "preserved",
                self.initialize(source, target, markers, new_profile=False),
            )
            self.assertEqual(before, digest_tree(target.parent))

    def test_bundle_update_or_deletion_does_not_change_marked_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            self.initialize(source, target, markers, new_profile=True)
            local_digest = digest_tree(target)

            (source / "sdd-test-role" / "SKILL.md").write_text(
                "new repository version\n", encoding="utf-8"
            )
            self.assertEqual(
                "preserved",
                self.initialize(source, target, markers, new_profile=False),
            )
            self.assertEqual(local_digest, digest_tree(target))

            for path in sorted(
                (source / "sdd-test-role").rglob("*"),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            (source / "sdd-test-role").rmdir()
            self.assertEqual(
                "preserved",
                self.initialize(source, target, markers, new_profile=False),
            )
            self.assertEqual(local_digest, digest_tree(target))

    def test_existing_profile_is_adopted_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            write_role(target, body="approved local version")
            before = digest_tree(target)

            self.assertEqual(
                "adopted",
                self.initialize(source, target, markers, new_profile=False),
            )
            self.assertEqual(before, digest_tree(target))
            self.assertTrue((markers / "tester").is_file())

    def test_existing_profile_without_unique_role_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            agent_skill = target / "agent-created"
            agent_skill.mkdir()
            (agent_skill / "SKILL.md").write_text("agent\n", encoding="utf-8")

            with self.assertRaises(INITIALIZER.SkillInitializationError) as caught:
                self.initialize(source, target, markers, new_profile=False)
            self.assertIn("requires exactly", str(caught.exception))
            self.assertFalse((target / "sdd-test-role").exists())
            self.assertFalse((markers / "tester").exists())

    def test_existing_profile_with_multiple_role_skills_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            write_role(target)
            write_role(target, name="sdd-unexpected-role")
            before = digest_tree(target)

            with self.assertRaises(INITIALIZER.SkillInitializationError) as caught:
                self.initialize(source, target, markers, new_profile=False)
            self.assertIn("requires exactly", str(caught.exception))
            self.assertEqual(before, digest_tree(target))
            self.assertFalse((markers / "tester").exists())

    def test_marked_profile_missing_role_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            self.initialize(source, target, markers, new_profile=True)
            role = target / "sdd-test-role"
            for path in sorted(
                role.rglob("*"), key=lambda item: len(item.parts), reverse=True
            ):
                if path.is_file():
                    path.unlink()
                else:
                    path.rmdir()
            role.rmdir()

            with self.assertRaises(INITIALIZER.SkillInitializationError) as caught:
                self.initialize(source, target, markers, new_profile=False)
            self.assertIn("restore it before startup", str(caught.exception))

    def test_new_profile_with_unknown_content_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            (target / "unknown").mkdir()

            with self.assertRaises(INITIALIZER.SkillInitializationError) as caught:
                self.initialize(source, target, markers, new_profile=True)
            self.assertIn("is not empty", str(caught.exception))
            self.assertFalse((markers / "tester").exists())

    def test_symlinked_role_or_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, target, markers = self.make_paths(temporary)
            write_role(source)
            outside = pathlib.Path(temporary) / "outside"
            outside.mkdir()
            (target / "sdd-test-role").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(INITIALIZER.SkillInitializationError):
                self.initialize(source, target, markers, new_profile=False)

            (target / "sdd-test-role").unlink()
            (markers / "tester").symlink_to(outside)
            with self.assertRaises(INITIALIZER.SkillInitializationError):
                self.initialize(source, target, markers, new_profile=False)


if __name__ == "__main__":
    unittest.main()
