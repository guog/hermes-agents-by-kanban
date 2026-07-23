import copy
import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "schemas" / "card-completion.schema.json"
VALIDATOR_PATH = ROOT / "scripts" / "validate_card_completion.py"
SPEC = importlib.util.spec_from_file_location(
    "completion_validator",
    VALIDATOR_PATH,
)
assert SPEC is not None and SPEC.loader is not None
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)

TASK_ID = "t_fixture123"
SHA = "a" * 40
DIGEST = "d" * 64
RUN_KEY = "sdd-" + ("a" * 20)


def completion_fixture(stage: str) -> dict:
    metadata = {
        "schema_version": 2,
        "run_key": RUN_KEY,
        "stage": stage,
        "outcome": "pass",
        "project_id": 123,
        "project_path": "group/project",
        "project_display_name": "测试项目",
        "checkout": "/workspace/projects/p123-project",
        "worktree": f"/workspace/projects/worktrees/p123/{RUN_KEY}",
        "branch": "feature/prd-example-aaaaaaaa",
        "target_branch": "main",
        "prd_path": "docs/prds/prd-example.md",
        "prd_commit_sha": SHA,
        "prd_mr_url": "https://gitlab.example/group/project/-/merge_requests/1",
        "kanban_card_id": TASK_ID,
        "mr_iid": None,
        "mr_url": None,
        "head_sha": None,
        "artifact_paths": [],
        "artifact_digest": None,
        "verification": ["fixture validation"],
        "residual_risk": [],
    }
    if stage in {"spec-review", "plan-review", "tasks-review"}:
        metadata.update(
            artifact_paths=[f"docs/{stage}.md"],
            artifact_digest=DIGEST,
            review_commit_sha=SHA,
        )
    if stage in {"test", "code-review", "merge"}:
        metadata.update(
            head_sha=SHA,
            mr_iid=2,
            mr_url="https://gitlab.example/group/project/-/merge_requests/2",
        )
    if stage == "merge":
        metadata.update(
            checked_head=SHA,
            merge_commit_sha=SHA,
        )
    return metadata


class CompletionValidatorTests(unittest.TestCase):
    def validate(self, metadata: object, *, task_id: str = TASK_ID) -> list[str]:
        return VALIDATOR.validate_completion_metadata(
            metadata,
            task_id=task_id,
            schema_path=SCHEMA,
        )

    def test_all_declared_stages_have_a_valid_fixture(self):
        for stage in VALIDATOR.load_schema(SCHEMA)["properties"]["stage"]["enum"]:
            with self.subTest(stage=stage):
                self.assertEqual([], self.validate(completion_fixture(stage)))

    def test_complete_spec_review_pass_is_valid(self):
        metadata = completion_fixture("spec-review")
        self.assertEqual([], self.validate(metadata))

    def test_missing_flat_project_and_worktree_context_is_rejected(self):
        metadata = completion_fixture("spec-review")
        for field in ("project_id", "project_path", "project_display_name", "checkout", "worktree"):
            metadata.pop(field)
        errors = self.validate(metadata)
        for field in ("project_id", "project_path", "project_display_name", "checkout", "worktree"):
            self.assertIn(f"$.{field}: is required", errors)

    def test_nested_workspace_does_not_replace_flat_worktree(self):
        metadata = completion_fixture("spec-review")
        worktree = metadata.pop("worktree")
        metadata["workspace"] = {"worktree": worktree}
        self.assertIn("$.worktree: is required", self.validate(metadata))

    def test_review_pass_requires_digest_paths_and_review_commit(self):
        metadata = completion_fixture("spec-review")
        metadata["artifact_paths"] = []
        metadata["artifact_digest"] = None
        metadata.pop("review_commit_sha")
        errors = self.validate(metadata)
        self.assertIn("$.artifact_paths: must contain at least 1 item(s)", errors)
        self.assertTrue(
            any(error.startswith("$.artifact_digest: expected string") for error in errors)
        )
        self.assertIn("$.review_commit_sha: is required", errors)

    def test_task_id_must_match_the_completing_card(self):
        metadata = completion_fixture("implement")
        errors = self.validate(metadata, task_id="t_other456")
        self.assertIn(
            "$.kanban_card_id: must equal the completing task id 't_other456'",
            errors,
        )

    def test_malformed_uri_is_a_validation_error(self):
        metadata = completion_fixture("implement")
        metadata["prd_mr_url"] = "https://["
        self.assertIn("$.prd_mr_url: must be an absolute URI", self.validate(metadata))

    def test_invalid_fixture_changes_do_not_mutate_the_input(self):
        metadata = completion_fixture("test")
        original = copy.deepcopy(metadata)
        self.validate(metadata)
        self.assertEqual(original, metadata)


class RuntimePatchContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.patch = (
            ROOT / "patches" / "hermes-0.19.0-dispatcher-kanban-guard.patch"
        ).read_text(encoding="utf-8")

    def test_formal_cards_are_validated_before_completion_writes(self):
        self.assertIn("SDD_COMPLETION_SCHEMA_MARKER", self.patch)
        self.assertIn(
            "_validate_formal_sdd_completion(conn, task_id, metadata)",
            self.patch,
        )
        self.assertLess(
            self.patch.index(
                "+    _validate_formal_sdd_completion(conn, task_id, metadata)"
            ),
            self.patch.index("     # Gate: verify created_cards BEFORE"),
        )

    def test_non_formal_cards_bypass_the_fleet_schema(self):
        self.assertIn(
            'if row is None or SDD_COMPLETION_SCHEMA_MARKER not in '
            '(row["body"] or ""):',
            self.patch,
        )
        self.assertIn("+        return", self.patch)

    def test_completion_and_edit_surface_structured_failures(self):
        for value in (
            "kanban_complete blocked:",
            "kanban: completion blocked:",
            "kanban: edit blocked:",
            "status_code=422",
        ):
            self.assertIn(value, self.patch)


if __name__ == "__main__":
    unittest.main()
