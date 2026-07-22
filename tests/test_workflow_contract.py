import os
import pathlib
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from workflow_contract import (  # noqa: E402
    ContractError,
    KanbanSimulation,
    SimCard,
    STAGE_ASSIGNEES,
    artifact_digest,
    code_gates_match,
    checked_head_for_merge,
    design_rework_target,
    derive_run_key,
    invalidated_gates,
    parse_start_command,
    permanent_blob_links,
    reconcile_run,
    validate_intake,
    validate_merge_completion,
    validate_one_to_one_artifact_keys,
    workspace_reconcile_action,
)


HOST = "gitlab.example.internal"
SHA = "a" * 40
BASE = f"https://{HOST}/group/project"
COMMAND = f"实现 PRD {BASE}/-/blob/{SHA}/docs/prds/prd-login.md {BASE}/-/merge_requests/7"


class IntakeContractTests(unittest.TestCase):
    def test_missing_and_mismatched_urls_do_not_parse(self):
        with self.assertRaisesRegex(ContractError, "请提供"):
            parse_start_command(f"实现 PRD {BASE}/-/merge_requests/7")
        with self.assertRaisesRegex(ContractError, "不属于同一项目"):
            parse_start_command(
                f"实现 PRD {BASE}/-/raw/{SHA}/docs/prds/prd-login.md "
                f"https://{HOST}/group/other/-/merge_requests/7"
            )

    def test_validation_failure_messages_are_distinct(self):
        blob, mr = parse_start_command(COMMAND)
        base = dict(
            blob=blob,
            mr=mr,
            expected_host=HOST,
            allowed_groups=["group"],
            project_accessible=True,
            file_exists=True,
            archived=False,
            mr_state="merged",
            mr_target_branch="main",
            default_branch="main",
            mr_merge_commit_sha=SHA,
            mr_contains_prd=True,
            current_prd_blob_sha="b" * 40,
            requested_prd_blob_sha="b" * 40,
        )
        cases = [
            ("project_accessible", False, "project_unreadable"),
            ("file_exists", False, "file_missing"),
            ("archived", True, "archived"),
            ("mr_state", "opened", "mr_not_merged"),
            ("mr_target_branch", "release", "wrong_target"),
            ("current_prd_blob_sha", "c" * 40, "stale_prd"),
        ]
        for field, value, code in cases:
            with self.subTest(field=field):
                kwargs = dict(base)
                kwargs[field] = value
                with self.assertRaises(ContractError) as caught:
                    validate_intake(**kwargs)
                self.assertEqual(code, caught.exception.code)

    def test_outside_group_is_rejected_before_clone(self):
        env = dict(os.environ, GITLAB_HOST=HOST, GITLAB_ALLOWED_GROUPS="allowed")
        result = subprocess.run(
            [
                str(ROOT / "scripts/prepare-run-workspace.sh"),
                "123",
                "group/project",
                f"https://{HOST}/group/project.git",
                "project",
                "sdd-abcdefghijklmnopqrst",
                "feature/prd-login-aaaaaaaa",
                SHA,
                "项目",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(3, result.returncode)
        self.assertIn("outside GITLAB_ALLOWED_GROUPS", result.stderr)


class KanbanContinuationTests(unittest.TestCase):
    RUN_KEY = "sdd-abcdefghijklmnopqrst"

    def create_gate(self, simulation: KanbanSimulation, key: str = "run-init") -> SimCard:
        gate = simulation.create_card(
            actor="dispatcher",
            idempotency_key=f"{self.RUN_KEY}:{key}:1:gate",
            stage="run-init",
            assignee="dispatcher",
            parent_ids=set(),
        )
        gate.status = "running"
        return gate

    def test_worker_continuation_state_chain_and_idempotency(self):
        simulation = KanbanSimulation()
        gate = self.create_gate(simulation)
        worker, continuation = simulation.create_dispatch_pair(
            actor="dispatcher",
            current_gate_id=gate.card_id,
            run_key=self.RUN_KEY,
            next_stage="spec-write",
            iteration=1,
        )
        self.assertEqual("blocked", worker.status)
        self.assertEqual("blocked", continuation.status)
        self.assertTrue(all(value is None for value in continuation.predicted_live_facts.values()))

        simulation.complete(gate.card_id)
        self.assertEqual("ready", worker.status)
        self.assertEqual("blocked", continuation.status)
        worker.status = "running"
        simulation.complete(worker.card_id)
        self.assertEqual("ready", continuation.status)

        duplicate_worker, duplicate_continuation = simulation.create_dispatch_pair(
            actor="dispatcher",
            current_gate_id=gate.card_id,
            run_key=self.RUN_KEY,
            next_stage="spec-write",
            iteration=1,
        )
        self.assertEqual(worker.card_id, duplicate_worker.card_id)
        self.assertEqual(continuation.card_id, duplicate_continuation.card_id)
        self.assertEqual(3, len(simulation.cards))

    def test_non_dispatcher_graph_writes_and_source_cards_are_rejected(self):
        simulation = KanbanSimulation()
        gate = self.create_gate(simulation)
        with self.assertRaises(ContractError) as caught:
            simulation.create_card(
                actor="coder",
                idempotency_key="forbidden",
                stage="implement",
                assignee="coder",
                parent_ids={gate.card_id},
            )
        self.assertEqual("dispatcher_required", caught.exception.code)
        with self.assertRaises(ContractError) as caught:
            simulation.link(actor="tester", parent_id=gate.card_id, child_id=gate.card_id)
        self.assertEqual("dispatcher_required", caught.exception.code)

        simulation.restore_card(
            SimCard(
                card_id="t_legacy",
                idempotency_key="legacy-worker-created",
                stage="implement",
                assignee="coder",
                created_by="coder",
                status="ready",
            )
        )
        ready_ids = [card.card_id for card in simulation.dispatchable_ready_cards()]
        self.assertNotIn("t_legacy", ready_ids)

    def test_worker_crash_retry_reuses_pair_and_respects_failure_limit(self):
        simulation = KanbanSimulation()
        gate = self.create_gate(simulation)
        worker, continuation = simulation.create_dispatch_pair(
            actor="dispatcher",
            current_gate_id=gate.card_id,
            run_key=self.RUN_KEY,
            next_stage="implement",
            iteration=1,
        )
        simulation.complete(gate.card_id)
        simulation.fail_attempt(worker.card_id)
        simulation.fail_attempt(worker.card_id)
        self.assertEqual("ready", worker.status)
        retry_worker, retry_continuation = simulation.create_dispatch_pair(
            actor="dispatcher",
            current_gate_id=gate.card_id,
            run_key=self.RUN_KEY,
            next_stage="implement",
            iteration=1,
        )
        self.assertEqual(worker.card_id, retry_worker.card_id)
        self.assertEqual(continuation.card_id, retry_continuation.card_id)
        simulation.fail_attempt(worker.card_id)
        self.assertEqual("blocked", worker.status)

    def test_every_work_review_rework_test_and_merge_stage_uses_a_pair(self):
        expected = {
            "spec-write", "spec-review", "spec-rework",
            "plan-write", "plan-review", "plan-rework",
            "tasks-write", "tasks-review", "tasks-rework",
            "implement", "test", "code-review", "code-rework", "merge",
        }
        self.assertEqual(expected, set(STAGE_ASSIGNEES))
        for stage in sorted(expected):
            with self.subTest(stage=stage):
                simulation = KanbanSimulation()
                gate = self.create_gate(simulation, key=stage)
                worker, continuation = simulation.create_dispatch_pair(
                    actor="dispatcher",
                    current_gate_id=gate.card_id,
                    run_key=self.RUN_KEY,
                    next_stage=stage,
                    iteration=1,
                )
                self.assertEqual({gate.card_id}, worker.parent_ids)
                self.assertEqual({worker.card_id}, continuation.parent_ids)
                self.assertTrue(worker.idempotency_key.endswith(":work"))
                self.assertTrue(continuation.idempotency_key.endswith(":continue"))
                if stage == "merge":
                    self.assertEqual("run-complete", continuation.stage)


class RecoveryAndGateTests(unittest.TestCase):
    def test_run_key_and_duplicate_recovery_are_deterministic(self):
        self.assertEqual(
            derive_run_key(HOST, 123, "docs/prds/prd-login.md", SHA),
            derive_run_key(HOST.upper(), 123, "docs/prds/prd-login.md", SHA),
        )
        self.assertEqual("resume", reconcile_run("running", "opened"))
        self.assertEqual("resume", reconcile_run("paused", "opened"))
        self.assertEqual("complete", reconcile_run("running", "merged"))
        self.assertEqual("start", reconcile_run(None, None))

    def test_dynamic_clone_and_worktree_recovery_are_idempotent(self):
        self.assertEqual(
            "clone",
            workspace_reconcile_action(
                checkout_exists=False,
                checkout_is_git=False,
                origin_matches=False,
                worktree_exists=False,
                worktree_branch_matches=False,
            ),
        )
        self.assertEqual(
            "create_worktree",
            workspace_reconcile_action(
                checkout_exists=True,
                checkout_is_git=True,
                origin_matches=True,
                worktree_exists=False,
                worktree_branch_matches=False,
            ),
        )
        self.assertEqual(
            "reuse",
            workspace_reconcile_action(
                checkout_exists=True,
                checkout_is_git=True,
                origin_matches=True,
                worktree_exists=True,
                worktree_branch_matches=True,
            ),
        )

    def test_artifact_digest_is_sorted_and_stage_scoped(self):
        first = artifact_digest([("b.md", "2" * 40), ("a.md", "1" * 40)])
        second = artifact_digest([("a.md", "1" * 40), ("b.md", "2" * 40)])
        self.assertEqual(first, second)
        self.assertEqual(
            {"spec", "plan", "tasks", "test", "code-review"},
            invalidated_gates("spec"),
        )
        self.assertNotIn("spec", invalidated_gates("plan"))
        self.assertEqual({"test", "code-review"}, invalidated_gates("code"))

    def test_multi_spec_gate_requires_complete_one_to_one_sets(self):
        self.assertEqual(
            ["api", "page"],
            validate_one_to_one_artifact_keys(
                ["x/specs/spec-page.md", "x/specs/spec-api.md"],
                ["x/plans/plan-api.md", "x/plans/plan-page.md"],
                ["x/tasks/task-page.md", "x/tasks/task-api.md"],
            ),
        )
        with self.assertRaisesRegex(ContractError, "一一对应"):
            validate_one_to_one_artifact_keys(
                ["x/specs/spec-page.md", "x/specs/spec-api.md"],
                ["x/plans/plan-page.md"],
                ["x/tasks/task-page.md", "x/tasks/task-api.md"],
            )

    def test_three_design_rework_routes_are_explicit(self):
        self.assertEqual("spec-writer", design_rework_target("spec", "fail"))
        self.assertEqual("planner", design_rework_target("plan", "fail"))
        self.assertEqual("tasker", design_rework_target("tasks", "fail"))
        self.assertEqual("spec-writer", design_rework_target("tasks", "scope_gap", "spec"))
        self.assertEqual("planner", design_rework_target("tasks", "scope_gap", "plan"))

    def test_code_push_invalidates_both_head_gates(self):
        old = "b" * 40
        self.assertTrue(code_gates_match(SHA, SHA, SHA))
        self.assertFalse(code_gates_match(SHA, old, SHA))
        self.assertFalse(code_gates_match(SHA, SHA, old))

    def test_checked_head_merge_blocks_pipeline_discussion_and_drift(self):
        base = dict(
            current_head=SHA,
            expected_head=SHA,
            mr_ready=True,
            mergeable=True,
            required_pipeline_status="success",
            blocking_discussions=0,
            artifact_gates_valid=True,
            tester_head=SHA,
            reviewer_head=SHA,
        )
        self.assertEqual(SHA, checked_head_for_merge(**base))
        for field, value, code in [
            ("expected_head", "b" * 40, "head_drift"),
            ("required_pipeline_status", "failed", "pipeline_not_successful"),
            ("blocking_discussions", 1, "blocking_discussions"),
            ("artifact_gates_valid", False, "artifact_gate_stale"),
        ]:
            with self.subTest(field=field):
                kwargs = dict(base)
                kwargs[field] = value
                with self.assertRaises(ContractError) as caught:
                    checked_head_for_merge(**kwargs)
                self.assertEqual(code, caught.exception.code)

    def test_merge_completion_and_api_are_bound_to_same_checked_head(self):
        validate_merge_completion(head_sha=SHA, checked_head=SHA, merge_api_sha=SHA)
        with self.assertRaises(ContractError) as caught:
            validate_merge_completion(
                head_sha=SHA,
                checked_head="b" * 40,
                merge_api_sha=SHA,
            )
        self.assertEqual("unchecked_merge", caught.exception.code)

    def test_permanent_links_use_merge_sha_and_sorted_paths(self):
        links = permanent_blob_links(BASE, SHA, ["z.md", "docs/prds/prd-login.md"])
        self.assertEqual(2, len(links))
        self.assertIn(f"/-/blob/{SHA}/docs/prds/prd-login.md", links[0])
        self.assertNotIn("main", links[0])


if __name__ == "__main__":
    unittest.main()
