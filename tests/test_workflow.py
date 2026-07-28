from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hollysys_controller.models import Stage, WorkMode
from hollysys_controller.workflow import (
    SEQUENCE,
    next_stage,
    protocol_retry_allowed,
    route_completion,
)
from tests.helpers import completion, config


class WorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = config(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_full_sequence_is_fixed(self) -> None:
        self.assertEqual(
            [stage.value for stage in SEQUENCE],
            [
                "spec-write",
                "spec-review",
                "plan-write",
                "plan-review",
                "tasks-write",
                "tasks-review",
                "implement",
                "test",
                "code-review",
            ],
        )
        self.assertEqual(next_stage(Stage.IMPLEMENT), Stage.TEST)

    def test_review_fail_returns_to_producer(self) -> None:
        metadata = completion(
            self.root,
            Stage.PLAN_REVIEW,
            outcome="fail",
            issues=["bad plan"],
            artifact_paths=["docs/plans/plan.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
        )
        route = route_completion(
            metadata,
            review_attempts_by_stage={Stage.PLAN_REVIEW: 1},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.PLAN_WRITE)

    def test_third_review_failure_routes_to_finalization(self) -> None:
        metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            outcome="fail",
            issues=["gap"],
            artifact_paths=["docs/specs/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
        )
        allowed = route_completion(
            metadata,
            review_attempts_by_stage={Stage.SPEC_REVIEW: 2},
            config=self.config,
        )
        finalization = route_completion(
            metadata,
            review_attempts_by_stage={Stage.SPEC_REVIEW: 3},
            config=self.config,
        )
        self.assertEqual(allowed.next_stage, Stage.SPEC_WRITE)
        self.assertEqual(allowed.next_mode, WorkMode.NORMAL)
        self.assertEqual(finalization.next_stage, Stage.SPEC_WRITE)
        self.assertEqual(finalization.next_mode, WorkMode.FINALIZATION)

    def test_finalization_pass_skips_fourth_review_and_enters_next_phase(self) -> None:
        final_review_url = (
            "https://gitlab.example.com/group/project/-/merge_requests/2#note_31"
        )
        decision_url = (
            "https://gitlab.example.com/group/project/-/merge_requests/2#note_32"
        )
        metadata = completion(
            self.root,
            Stage.SPEC_WRITE,
            mode="finalization",
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            artifact_paths=["docs/specs/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="forced_after_review_limit",
            gitlab_urls=[decision_url],
            key_decisions=["Use the stricter acceptance rule"],
            residual_risk=["Legacy clients may require adaptation"],
            forced_advance={
                "review_limit": 3,
                "final_review_card_id": "t_review",
                "final_review_url": final_review_url,
                "decision_url": decision_url,
                "baseline_commit_sha": "c" * 40,
                "artifact_paths": ["docs/specs/spec.md"],
                "artifact_digest": "b" * 64,
                "key_decisions": ["Use the stricter acceptance rule"],
                "unresolved_findings": ["PRD rules conflict"],
                "residual_risks": ["Legacy clients may require adaptation"],
            },
        )

        route = route_completion(
            metadata,
            review_attempts_by_stage={Stage.SPEC_REVIEW: 3},
            config=self.config,
        )

        self.assertEqual(route.next_stage, Stage.PLAN_WRITE)
        self.assertNotEqual(route.next_stage, Stage.SPEC_REVIEW)

    def test_test_failure_still_routes_to_code_review(self) -> None:
        metadata = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["browser assertion failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        route = route_completion(
            metadata,
            review_attempts_by_stage={},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.CODE_REVIEW)

    def test_code_gate_failure_returns_to_implement_within_limit(self) -> None:
        test = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["browser assertion failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        route = route_completion(
            review,
            review_attempts_by_stage={},
            config=self.config,
            paired_test=test,
            code_modifications=4,
        )
        self.assertEqual(route.next_stage, Stage.IMPLEMENT)

    def test_both_code_gates_must_pass_same_head(self) -> None:
        test = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        route = route_completion(
            review,
            review_attempts_by_stage={},
            config=self.config,
            paired_test=test,
        )
        self.assertTrue(route.merge)

        stale_test = test.model_copy(update={"head_sha": "e" * 40})
        stale = route_completion(
            review,
            review_attempts_by_stage={},
            config=self.config,
            paired_test=stale_test,
        )
        self.assertEqual(stale.next_stage, Stage.TEST)

    def test_fifth_failed_modification_ends_automatic_flow(self) -> None:
        test = completion(
            self.root,
            Stage.TEST,
            outcome="pass",
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="fail",
            issues=["defect remains"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        route = route_completion(
            review,
            review_attempts_by_stage={},
            config=self.config,
            paired_test=test,
            code_modifications=5,
        )
        self.assertIsNone(route.next_stage)
        self.assertIn("limit 5 exhausted", route.blocked_reason or "")

    def test_stale_code_gate_cancellation_restarts_test(self) -> None:
        metadata = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="cancelled",
            issues=["MR head changed during review"],
        )
        route = route_completion(
            metadata,
            review_attempts_by_stage={Stage.CODE_REVIEW: 1},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.TEST)

    def test_protocol_budget_allows_two_retries(self) -> None:
        self.assertTrue(protocol_retry_allowed(1, self.config))
        self.assertTrue(protocol_retry_allowed(2, self.config))
        self.assertFalse(protocol_retry_allowed(3, self.config))


if __name__ == "__main__":
    unittest.main()
