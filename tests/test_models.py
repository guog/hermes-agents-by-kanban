from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from hollysys_controller.models import (
    CompletionMetadata,
    ResolveRequest,
    Stage,
    validate_persisted_completion_metadata,
)
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

    def test_runtime_worker_session_stamp_is_not_a_worker_schema_field(self) -> None:
        payload = completion(self.root).model_dump(mode="json")
        payload["worker_session_id"] = "20260728_122745_220289"

        with self.assertRaises(ValidationError):
            CompletionMetadata.model_validate(payload)

        parsed = validate_persisted_completion_metadata(payload)
        self.assertEqual(parsed.kanban_card_id, payload["kanban_card_id"])

    def test_persisted_completion_rejects_other_extras_and_invalid_stamp(self) -> None:
        payload = completion(self.root).model_dump(mode="json")
        payload["worker_session_id"] = "session-1"
        payload["unexpected_runtime_field"] = True
        with self.assertRaises(ValidationError):
            validate_persisted_completion_metadata(payload)

        payload.pop("unexpected_runtime_field")
        payload["worker_session_id"] = ""
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            validate_persisted_completion_metadata(payload)

    def test_persisted_review_discards_non_authoritative_repository_evidence(
        self,
    ) -> None:
        payload = completion(
            self.root,
            Stage.PLAN_REVIEW,
            artifact_paths=["docs/plans/feature/plan.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="reviewed",
        ).model_dump(mode="json")
        payload["repository_evidence"] = completion(
            self.root,
            Stage.PLAN_WRITE,
        ).repository_evidence.model_dump(mode="json")

        with self.assertRaises(ValidationError):
            CompletionMetadata.model_validate(payload)

        parsed = validate_persisted_completion_metadata(payload)
        self.assertIsNone(parsed.repository_evidence)

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

    def test_scope_gap_is_rejected_and_fail_requires_issues(self) -> None:
        payload = completion(self.root).model_dump(mode="json")
        payload["outcome"] = "scope_gap"
        with self.assertRaises(ValidationError):
            CompletionMetadata.model_validate(payload)
        with self.assertRaises(ValidationError):
            completion(self.root, Stage.SPEC_REVIEW, outcome="fail")

    def test_review_pass_requires_digest_contract(self) -> None:
        with self.assertRaises(ValidationError):
            completion(self.root, Stage.SPEC_REVIEW)
        valid = completion(
            self.root,
            Stage.SPEC_REVIEW,
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="reviewed",
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

    def test_unavailable_test_condition_is_a_structured_skip(self) -> None:
        valid = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            test_disposition="skipped_unavailable",
            skip_reason="browser runtime is not installed in the tester container",
            verification=["unit tests passed", "browser binary preflight failed"],
            residual_risk=["browser interaction remains unverified"],
        )
        self.assertEqual(valid.test_disposition.value, "skipped_unavailable")

        payload = valid.model_dump(mode="json")
        payload["outcome"] = "fail"
        payload["issues"] = ["browser test could not run"]
        with self.assertRaisesRegex(ValidationError, "skipped test requires pass"):
            CompletionMetadata.model_validate(payload)

        missing_reason = valid.model_dump(mode="json")
        missing_reason["skip_reason"] = None
        with self.assertRaisesRegex(ValidationError, "skipped test requires pass"):
            CompletionMetadata.model_validate(missing_reason)

    def test_non_test_rejects_test_disposition_from_lint_or_build(self) -> None:
        payload = completion(self.root, Stage.PLAN_WRITE).model_dump(mode="json")
        payload["test_disposition"] = "executed"
        with self.assertRaisesRegex(
            ValidationError, "only valid for test pass/fail"
        ):
            CompletionMetadata.model_validate(payload)

        payload["test_disposition"] = None
        payload["skip_reason"] = "build succeeded"
        with self.assertRaisesRegex(
            ValidationError, "only valid for test pass/fail"
        ):
            CompletionMetadata.model_validate(payload)

    def test_authoring_pass_requires_repository_evidence(self) -> None:
        valid = completion(self.root, Stage.IMPLEMENT)
        self.assertEqual(
            valid.repository_evidence.repository_base_sha,
            "9" * 40,
        )
        self.assertEqual(
            valid.repository_evidence.change_strategy.value,
            "extend_existing",
        )

        payload = valid.model_dump(mode="json")
        payload["repository_evidence"] = None
        with self.assertRaisesRegex(
            ValidationError, "authoring pass requires repository_evidence"
        ):
            CompletionMetadata.model_validate(payload)

        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        ).model_dump(mode="json")
        review["repository_evidence"] = valid.repository_evidence.model_dump(
            mode="json"
        )
        with self.assertRaisesRegex(
            ValidationError, "only valid for an authoring pass"
        ):
            CompletionMetadata.model_validate(review)

        payload = valid.model_dump(mode="json")
        payload["repository_evidence"]["inspected_paths"] = ["../other-repo"]
        with self.assertRaisesRegex(
            ValidationError, "exact repository-relative paths"
        ):
            CompletionMetadata.model_validate(payload)

    def test_finalization_requires_matching_forced_baseline_and_risks(self) -> None:
        forced = {
            "review_limit": 3,
            "final_review_card_id": "t_review",
            "final_review_url": (
                "https://gitlab.example.com/group/project/-/merge_requests/2#note_31"
            ),
            "decision_url": (
                "https://gitlab.example.com/group/project/-/merge_requests/2#note_32"
            ),
            "baseline_commit_sha": "c" * 40,
            "artifact_paths": ["docs/specs/feature/spec.md"],
            "artifact_digest": "b" * 64,
            "key_decisions": ["Prefer the safer validation rule"],
            "unresolved_findings": ["PRD rules conflict"],
            "residual_risks": ["compatibility requires follow-up"],
        }
        valid = completion(
            self.root,
            Stage.SPEC_WRITE,
            mode="finalization",
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="forced_after_review_limit",
            forced_advance=forced,
            gitlab_urls=[forced["decision_url"]],
            key_decisions=["Prefer the safer validation rule"],
            residual_risk=["compatibility requires follow-up"],
        )
        self.assertEqual(valid.forced_advance.review_limit, 3)

        payload = valid.model_dump(mode="json")
        payload["forced_advance"]["artifact_digest"] = "e" * 64
        with self.assertRaisesRegex(
            ValidationError, "forced_advance baseline"
        ):
            CompletionMetadata.model_validate(payload)

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
        self.assertEqual(len(conditions), 8)
        self.assertEqual(conditions[0]["then"]["properties"]["issues"]["minItems"], 1)
        self.assertEqual(
            conditions[1]["then"]["properties"]["artifact_paths"]["minItems"],
            1,
        )
        self.assertIn("head_sha", conditions[3]["then"]["required"])
        self.assertIn("test_disposition", conditions[4]["then"]["required"])
        self.assertEqual(
            conditions[4]["else"]["properties"]["test_disposition"],
            {"type": "null"},
        )
        self.assertEqual(
            conditions[4]["else"]["properties"]["skip_reason"],
            {"type": "null"},
        )
        self.assertIn("repository_evidence", conditions[5]["then"]["required"])
        self.assertIn("skip_reason", conditions[6]["then"]["required"])
        self.assertEqual(
            conditions[6]["else"]["properties"]["skip_reason"],
            {"type": "null"},
        )
        self.assertIn("forced_advance", conditions[7]["then"]["required"])


if __name__ == "__main__":
    unittest.main()
