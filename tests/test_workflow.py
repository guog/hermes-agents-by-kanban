from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hollysys_controller.models import Stage
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
            self.root, Stage.PLAN_REVIEW, outcome="fail", issues=["bad plan"]
        )
        route = route_completion(
            metadata,
            attempts_by_stage={Stage.PLAN_WRITE: 1},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.PLAN_WRITE)

    def test_design_budget_allows_three_reworks_then_blocks(self) -> None:
        metadata = completion(
            self.root, Stage.SPEC_REVIEW, outcome="fail", issues=["gap"]
        )
        allowed = route_completion(
            metadata,
            attempts_by_stage={Stage.SPEC_WRITE: 3},
            config=self.config,
        )
        exhausted = route_completion(
            metadata,
            attempts_by_stage={Stage.SPEC_WRITE: 4},
            config=self.config,
        )
        self.assertEqual(allowed.next_stage, Stage.SPEC_WRITE)
        self.assertIn("budget exhausted", exhausted.blocked_reason or "")

    def test_code_budget_is_aggregate_implement_attempts(self) -> None:
        metadata = completion(
            self.root, Stage.CODE_REVIEW, outcome="fail", issues=["defect"]
        )
        allowed = route_completion(
            metadata,
            attempts_by_stage={Stage.IMPLEMENT: 5},
            config=self.config,
        )
        exhausted = route_completion(
            metadata,
            attempts_by_stage={Stage.IMPLEMENT: 6},
            config=self.config,
        )
        self.assertEqual(allowed.next_stage, Stage.IMPLEMENT)
        self.assertIn("code rework", exhausted.blocked_reason or "")

    def test_scope_gap_routes_to_explicit_target(self) -> None:
        metadata = completion(
            self.root,
            Stage.TEST,
            outcome="scope_gap",
            scope_gap_target="tasks-write",
            issues=["missing acceptance task"],
        )
        route = route_completion(
            metadata,
            attempts_by_stage={Stage.TASKS_WRITE: 1},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.TASKS_WRITE)

    def test_stale_code_gate_cancellation_restarts_test(self) -> None:
        metadata = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="cancelled",
            issues=["MR head changed during review"],
        )
        route = route_completion(
            metadata,
            attempts_by_stage={Stage.IMPLEMENT: 1},
            config=self.config,
        )
        self.assertEqual(route.next_stage, Stage.TEST)

    def test_protocol_budget_allows_two_retries(self) -> None:
        self.assertTrue(protocol_retry_allowed(1, self.config))
        self.assertTrue(protocol_retry_allowed(2, self.config))
        self.assertFalse(protocol_retry_allowed(3, self.config))


if __name__ == "__main__":
    unittest.main()
