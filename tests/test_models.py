from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from hollysys_controller.models import CompletionMetadata, ResolveRequest, Stage
from scripts.generate_completion_schema import generated_schema
from tests.helpers import completion


class CompletionMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_extra_fields_are_rejected(self) -> None:
        payload = completion(self.root).model_dump(mode="json")
        payload["next_card_ids"] = ["t_fake"]
        with self.assertRaises(ValidationError):
            CompletionMetadata.model_validate(payload)

    def test_run_key_accepts_all_lowercase_alphanumeric_characters(self) -> None:
        run_key = "hollysys-a0b1c2d3e4f5g6h7i8j9"
        payload = completion(self.root).model_dump(mode="json")
        payload["run_key"] = run_key
        self.assertEqual(
            CompletionMetadata.model_validate(payload).run_key,
            run_key,
        )

        resolution = ResolveRequest(
            run_key=run_key,
            card_id="t_card-1",
            block_id=f"{run_key}:t_card-1:1",
            message_id="om_message-1",
            sender="ou_sender-1",
            chat_id="oc_chat-1",
            answer="continue",
        )
        self.assertEqual(resolution.block_id, f"{run_key}:t_card-1:1")

    def test_run_key_rejects_non_lowercase_alphanumeric_characters(self) -> None:
        payload = completion(self.root).model_dump(mode="json")
        for invalid in (
            "hollysys-A0b1c2d3e4f5g6h7i8j9",
            "hollysys-a0b1c2d3e4f5g6h7i8j_",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                CompletionMetadata.model_validate({**payload, "run_key": invalid})

    def test_scope_gap_requires_target_and_issues(self) -> None:
        with self.assertRaises(ValidationError):
            completion(self.root, outcome="scope_gap")
        valid = completion(
            self.root,
            outcome="scope_gap",
            scope_gap_target="plan-write",
            issues=["PLAN omits rollback behavior"],
        )
        self.assertEqual(valid.scope_gap_target, "plan-write")

    def test_review_pass_requires_digest_contract(self) -> None:
        with self.assertRaises(ValidationError):
            completion(self.root, Stage.SPEC_REVIEW)
        valid = completion(
            self.root,
            Stage.SPEC_REVIEW,
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="b" * 64,
            review_commit_sha="c" * 40,
        )
        self.assertEqual(valid.artifact_digest, "b" * 64)

    def test_code_gate_pass_requires_current_head_fields(self) -> None:
        with self.assertRaises(ValidationError):
            completion(self.root, Stage.TEST)
        valid = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        self.assertEqual(valid.head_sha, "d" * 40)

    def test_committed_schema_matches_model(self) -> None:
        path = (
            Path(__file__).resolve().parent.parent
            / "schemas"
            / "card-completion.schema.json"
        )
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")), generated_schema()
        )

    def test_generated_schema_contains_cross_field_contracts(self) -> None:
        schema = generated_schema()
        self.assertEqual(
            schema["properties"]["run_key"]["pattern"],
            r"^hollysys-[a-z0-9]{20}$",
        )
        conditions = schema["allOf"]
        self.assertEqual(len(conditions), 4)
        self.assertEqual(conditions[0]["then"]["properties"]["issues"]["minItems"], 1)
        self.assertEqual(
            conditions[2]["then"]["properties"]["artifact_paths"]["minItems"],
            1,
        )
        self.assertIn("head_sha", conditions[3]["then"]["required"])


if __name__ == "__main__":
    unittest.main()
