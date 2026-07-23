import pathlib
import sys
import tempfile
import types
import typing
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PATCH_PATH = ROOT / "patches" / "hermes-0.19.0-dispatcher-kanban-guard.patch"
MUTATIONS = ("edit", "patch", "delete", "write_file", "remove_file")


def load_guard(skills_dir: pathlib.Path):
    patch = PATCH_PATH.read_text(encoding="utf-8")
    section = patch.split(
        "diff --git a/tools/skill_manager_tool.py b/tools/skill_manager_tool.py\n",
        1,
    )[1]
    first_hunk = section.split("@@ -433,8 +433,79 @@\n", 1)[1].split(
        "@@ -1355,5 +1426,9 @@\n", 1
    )[0]
    source = "\n".join(
        line[1:] for line in first_hunk.splitlines() if line.startswith("+")
    )

    logger = types.SimpleNamespace(warning=lambda *args, **kwargs: None)
    namespace = {
        "Any": typing.Any,
        "Dict": typing.Dict,
        "Optional": typing.Optional,
        "logger": logger,
        "_skills_dir": lambda: skills_dir,
    }
    exec(compile(source, str(PATCH_PATH), "exec"), namespace)
    return namespace["_fleet_skill_mutation_guard"]


class RuntimeSkillGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.skills = pathlib.Path(self.temporary.name) / "skills"
        self.skills.mkdir()
        tools = types.ModuleType("tools")
        skill_usage = types.ModuleType("tools.skill_usage")
        skill_usage.is_bundled = lambda name: name == "builtin-skill"
        tools.skill_usage = skill_usage
        self.previous_tools = sys.modules.get("tools")
        self.previous_skill_usage = sys.modules.get("tools.skill_usage")
        sys.modules["tools"] = tools
        sys.modules["tools.skill_usage"] = skill_usage
        self.addCleanup(self.restore_modules)
        self.guard = load_guard(self.skills)

    def restore_modules(self):
        if self.previous_tools is None:
            sys.modules.pop("tools", None)
        else:
            sys.modules["tools"] = self.previous_tools
        if self.previous_skill_usage is None:
            sys.modules.pop("tools.skill_usage", None)
        else:
            sys.modules["tools.skill_usage"] = self.previous_skill_usage

    def write_manifest(self):
        (self.skills / ".bundled_manifest").write_text(
            "builtin-skill:origin-hash\n", encoding="utf-8"
        )

    def test_all_existing_mutations_reject_bundled_builtin(self):
        self.write_manifest()
        for action in MUTATIONS:
            with self.subTest(action=action):
                result = self.guard(action, "builtin-skill")
                self.assertFalse(result["success"])
                self.assertTrue(result["_fleet_builtin_immutable"])
                self.assertIn(action, result["error"])

    def test_role_skill_can_change_but_cannot_be_deleted(self):
        self.write_manifest()
        for action in ("edit", "patch", "write_file", "remove_file"):
            with self.subTest(action=action):
                self.assertIsNone(self.guard(action, "sdd-test"))
        result = self.guard("delete", "sdd-test")
        self.assertFalse(result["success"])
        self.assertTrue(result["_fleet_role_skill_protected"])

    def test_agent_created_skill_keeps_normal_mutation_behavior(self):
        self.write_manifest()
        for action in MUTATIONS:
            with self.subTest(action=action):
                self.assertIsNone(self.guard(action, "agent-created"))
        self.assertIsNone(self.guard("create", "agent-created"))

    def test_missing_or_empty_manifest_fails_closed_for_existing_mutations(self):
        manifest = self.skills / ".bundled_manifest"
        for state in ("missing", "empty"):
            if state == "empty":
                manifest.write_text("", encoding="utf-8")
            for action in MUTATIONS:
                with self.subTest(state=state, action=action):
                    result = self.guard(action, "agent-created")
                    self.assertFalse(result["success"])
                    self.assertTrue(result["_fleet_fail_closed"])
        self.assertIsNone(self.guard("create", "agent-created"))


if __name__ == "__main__":
    unittest.main()
