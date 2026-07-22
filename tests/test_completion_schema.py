import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "schemas" / "card-completion.schema.json").read_text(encoding="utf-8"))


def condition_for(stages: set[str]) -> dict:
    for condition in SCHEMA["allOf"]:
        stage_rule = condition["if"]["properties"]["stage"]
        actual = set(stage_rule.get("enum", [stage_rule.get("const")]))
        if actual == stages:
            return condition
    raise AssertionError(f"condition not found for {sorted(stages)}")


class CompletionSchemaTests(unittest.TestCase):
    def test_design_review_pass_requires_digest_and_review_commit(self):
        condition = condition_for({"spec-review", "plan-review", "tasks-review"})
        then = condition["then"]
        self.assertTrue({"artifact_paths", "artifact_digest", "review_commit_sha"} <= set(then["required"]))
        self.assertEqual("string", then["properties"]["artifact_digest"]["type"])
        self.assertEqual("string", then["properties"]["review_commit_sha"]["type"])
        self.assertEqual(1, then["properties"]["artifact_paths"]["minItems"])

    def test_test_and_code_review_pass_require_exact_head_and_mr(self):
        condition = condition_for({"test", "code-review"})
        then = condition["then"]
        self.assertTrue({"head_sha", "mr_iid", "mr_url"} <= set(then["required"]))
        self.assertEqual("^[0-9a-f]{40}$", then["properties"]["head_sha"]["pattern"])
        self.assertEqual("string", then["properties"]["head_sha"]["type"])

    def test_merge_pass_requires_checked_head_and_merge_sha(self):
        condition = condition_for({"merge"})
        then = condition["then"]
        self.assertTrue(
            {"head_sha", "checked_head", "mr_iid", "mr_url"}
            <= set(then["required"])
        )
        self.assertIn("head_sha equals checked_head", then["$comment"])
        self.assertIn("sha=checked_head", then["$comment"])
        merge_pass = next(
            item
            for item in SCHEMA["allOf"]
            if item["if"]["properties"].get("stage", {}).get("const") == "merge"
            and item["if"]["properties"].get("outcome", {}).get("const") == "pass"
        )
        self.assertIn("merge_commit_sha", merge_pass["then"]["required"])


if __name__ == "__main__":
    unittest.main()
