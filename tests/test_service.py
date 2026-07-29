from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from pydantic import ValidationError

from hollysys_controller.kanban import (
    EventRecord,
    TaskRecord,
    render_card_body,
    render_run_body,
)
from hollysys_controller.models import (
    BaselineDisposition,
    CardRecord,
    CompletionMetadata,
    NotificationLevel,
    Phase,
    RepairContext,
    RepairKind,
    Stage,
    WorkMode,
)
from hollysys_controller.service import ControllerService, HistoryItem
from hollysys_controller.store import ControllerStore, ManagedCard
from tests.helpers import completion, config, run_record


def task_record(
    *,
    task_id: str,
    body: str,
    status: str,
    assignee: str | None = None,
    idempotency_key: str | None = None,
    tenant: str | None = None,
    skills: list[str] | None = None,
    parents: list[str] | None = None,
    comments: list[dict] | None = None,
    latest_outcome: str | None = None,
    event_kinds: list[str] | None = None,
    latest_metadata: dict | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        title=task_id,
        body=body,
        assignee=assignee,
        status=status,
        created_by="hollysys-controller",
        created_at=1,
        completed_at=2 if status == "done" else None,
        idempotency_key=idempotency_key,
        tenant=tenant,
        workspace_path=None,
        branch_name=None,
        skills=skills or [],
        current_run_id=None,
        latest_summary=None,
        latest_metadata=latest_metadata,
        latest_outcome=latest_outcome,
        parents=parents or [],
        comments=comments or [],
        event_kinds=event_kinds or [],
    )


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = run_record(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_resolve_reuses_retry_created_before_request_commit(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=2,
            idempotency_key=f"{self.run.run_key}:implement:2:work",
            parent_card_id="t_blocked",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
            resume_answer="use the approved interface",
            resumed_from_card_id="t_blocked",
        )
        task = task_record(
            task_id="t_retry",
            body=render_card_body(card),
            status="todo",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        found = service._resolved_retry(
            [HistoryItem(managed, task)],
            "t_blocked",
            Stage.IMPLEMENT,
            WorkMode.NORMAL,
            "use the approved interface",
        )
        self.assertEqual(found, task)

    def test_resolve_triage_before_publishing_retry(self) -> None:
        block_id = f"{self.run.run_key}:t_blocked:7"
        comment = (
            "[human-block:v1]\n"
            f"block_id: {block_id}\n"
            "kind: environment\n"
            "summary: target database evidence is unavailable\n"
            "evidence: target snapshot is missing\n"
            "required_action: provide an explicit safe implementation boundary\n"
            "resume_check: the answer preserves the deployment gate\n"
            "gate_phase: deployment\n"
            "requirement_ids: BLK-001\n"
            "contract_refs: PLAN-BLK-001"
        )
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:implement:1:normal:work",
            parent_card_id="t_tasks_review",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
        )
        triage = task_record(
            task_id="t_blocked",
            body=render_card_body(card),
            status="triage",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
            comments=[{"body": comment}],
            latest_outcome="blocked",
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=triage.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        retry_card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=2,
            idempotency_key=f"{self.run.run_key}:implement:2:normal:work",
            parent_card_id=triage.id,
            assignee="coder",
            skills=["hollysys-implement", "glab"],
            resume_answer="continue local implementation; preserve deployment gates",
            resumed_from_card_id=triage.id,
        )
        retry = task_record(
            task_id="t_retry",
            body=render_card_body(retry_card),
            status="blocked",
            assignee=retry_card.assignee,
            idempotency_key=retry_card.idempotency_key,
            tenant=self.run.run_key,
            skills=retry_card.skills,
            parents=[triage.id],
        )
        service = object.__new__(ControllerService)
        service._lock = threading.RLock()
        service.store = ControllerStore(self.root / "controller.db")
        service._history = lambda _: ([HistoryItem(managed, triage)], self.run)
        calls: list[tuple] = []

        def create_retry(*args, **kwargs):
            calls.append(("create", kwargs["publish"]))
            return retry

        def publish_retry(run, task):
            self.assertIn(("complete", triage.id), calls)
            calls.append(("publish", task.id))
            return task_record(
                task_id=task.id,
                body=task.body or "",
                status="ready",
                assignee=task.assignee,
                idempotency_key=task.idempotency_key,
                tenant=task.tenant,
                skills=task.skills,
                parents=task.parents,
            )

        service._create_work = create_retry
        service._ensure_work_published = publish_retry
        service._controller_completion = lambda *args, **kwargs: {
            "outcome": "cancelled"
        }
        service.kanban = type(
            "TriageKanban",
            (),
            {
                "comment": staticmethod(
                    lambda board, task_id, text, author: calls.append(
                        ("comment", task_id)
                    )
                ),
                "prepare_human_block_for_completion": staticmethod(
                    lambda board, task_id: calls.append(("prepare", task_id))
                ),
                "complete": staticmethod(
                    lambda board, task_id, summary, metadata: calls.append(
                        ("complete", task_id)
                    )
                ),
            },
        )()
        answer = "continue local implementation; preserve deployment gates"

        result = service.resolve(
            {
                "run_key": self.run.run_key,
                "card_id": triage.id,
                "block_id": block_id,
                "message_id": "om_reply",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
                "answer": answer,
            }
        )

        self.assertEqual(result["resolved_card"], triage.id)
        self.assertEqual(result["new_card"], retry.id)
        self.assertEqual(
            calls,
            [
                ("create", False),
                ("comment", triage.id),
                ("prepare", triage.id),
                ("complete", triage.id),
                ("publish", retry.id),
            ],
        )
        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event"], "resumed")

    def test_reconcile_recovers_completed_root_without_first_card(self) -> None:
        root = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=root.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: ([HistoryItem(managed, root)], self.run)
        service.gitlab = type(
            "NoMergeRequest",
            (),
            {"delivery_mr": staticmethod(lambda run: None)},
        )()
        created: list[tuple[Stage, str]] = []
        service._create_work = lambda run, stage, parent: created.append(
            (stage, parent)
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(created, [(Stage.SPEC_WRITE, "t_root")])

    def test_reconcile_releases_interrupted_controller_hold(self) -> None:
        root = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_WRITE,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:spec-write:1:work",
            parent_card_id=root.id,
            assignee="spec-writer",
            skills=["hollysys-write-spec", "glab"],
        )
        held = task_record(
            task_id="t_held",
            body=render_card_body(card),
            status="blocked",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[root.id],
            event_kinds=["created", "blocked"],
        )
        root_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=root.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        held_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=held.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=root.id,
            purpose="work",
            created_at=2,
        )
        service = object.__new__(ControllerService)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: (
            [
                HistoryItem(root_managed, root),
                HistoryItem(held_managed, held),
            ],
            self.run,
        )
        service.gitlab = type(
            "NoMergeRequest",
            (),
            {"delivery_mr": staticmethod(lambda run: None)},
        )()
        service.reader = type("Reader", (), {})()
        released: list[str] = []
        service.kanban = type(
            "ReleaseRecorder",
            (),
            {"release": staticmethod(lambda board, task_id: released.append(task_id))},
        )()

        service.reconcile_run(self.run.run_key)

        self.assertEqual(released, ["t_held"])

    def test_reconcile_test_failure_still_creates_code_review(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.TEST,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:test:1:normal:work",
            parent_card_id="t_implement",
            assignee="tester",
            skills=["hollysys-test", "glab"],
        )
        metadata = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["browser assertion failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_test",
        )
        task = task_record(
            task_id="t_test",
            body=render_card_body(card),
            status="done",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
            latest_metadata=metadata.model_dump(mode="json"),
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.TEST.value,
            iteration=1,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: ([HistoryItem(managed, task)], self.run)
        service.gitlab = type(
            "CurrentHead",
            (),
            {
                "validate_gate": staticmethod(lambda run, metadata: "id:9"),
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {"iid": 2, "sha": "d" * 40}
                ),
            },
        )()
        service._frozen_violation = lambda run, history, ref: None
        service._review_attempts_by_stage = lambda history: {}
        service._code_modification_count = lambda history: 0
        created: list[tuple[Stage, RepairContext | None]] = []
        service._create_work = (
            lambda run, stage, parent, **kwargs: created.append(
                (stage, kwargs.get("repair_context"))
            )
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(created, [(Stage.CODE_REVIEW, None)])

    def test_reconcile_code_review_aggregates_both_gate_findings(self) -> None:
        test_card = CardRecord(
            run=self.run,
            stage=Stage.TEST,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:test:1:normal:work",
            parent_card_id="t_implement",
            assignee="tester",
            skills=["hollysys-test", "glab"],
        )
        test_metadata = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["browser assertion failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_test",
        )
        test_task = task_record(
            task_id="t_test",
            body=render_card_body(test_card),
            status="done",
            assignee=test_card.assignee,
            idempotency_key=test_card.idempotency_key,
            tenant=self.run.run_key,
            skills=test_card.skills,
            parents=[test_card.parent_card_id],
            latest_metadata=test_metadata.model_dump(mode="json"),
        )
        test_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=test_task.id,
            run_key=self.run.run_key,
            stage=Stage.TEST.value,
            iteration=1,
            idempotency_key=test_card.idempotency_key,
            parent_card_id=test_card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        review_card = CardRecord(
            run=self.run,
            stage=Stage.CODE_REVIEW,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:code-review:1:normal:work",
            parent_card_id=test_task.id,
            assignee="code-reviewer",
            skills=["hollysys-review-code", "glab"],
        )
        review_metadata = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="fail",
            issues=["P&ID redraws the full canvas"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_review",
        )
        review_task = task_record(
            task_id="t_review",
            body=render_card_body(review_card),
            status="done",
            assignee=review_card.assignee,
            idempotency_key=review_card.idempotency_key,
            tenant=self.run.run_key,
            skills=review_card.skills,
            parents=[test_task.id],
            latest_metadata=review_metadata.model_dump(mode="json"),
        )
        review_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=review_task.id,
            run_key=self.run.run_key,
            stage=Stage.CODE_REVIEW.value,
            iteration=1,
            idempotency_key=review_card.idempotency_key,
            parent_card_id=test_task.id,
            purpose="work",
            created_at=2,
        )
        history = [
            HistoryItem(test_managed, test_task),
            HistoryItem(review_managed, review_task),
        ]
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: (history, self.run)
        service.gitlab = type(
            "CurrentHead",
            (),
            {
                "validate_gate": staticmethod(lambda run, metadata: "id:9"),
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {"iid": 2, "sha": "d" * 40}
                ),
            },
        )()
        service._frozen_violation = lambda run, history, ref: None
        service._review_attempts_by_stage = lambda history: {}
        service._code_modification_count = lambda history: 2
        service._enqueue_code_retry = lambda *args: None
        created: list[tuple[Stage, RepairContext | None]] = []
        service._create_work = (
            lambda run, stage, parent, **kwargs: created.append(
                (stage, kwargs.get("repair_context"))
            )
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(created[0][0], Stage.IMPLEMENT)
        context = created[0][1]
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.code_modification, 3)
        self.assertEqual(context.related_card_ids, ["t_test", "t_review"])
        self.assertEqual(
            context.issues,
            [
                "[tester] browser assertion failed",
                "[code-reviewer] P&ID redraws the full canvas",
            ],
        )

    def test_failure_limit_notification_is_idempotent(self) -> None:
        service = object.__new__(ControllerService)
        service._lock = threading.RLock()
        service.store = ControllerStore(self.root / "controller.db")
        failed_task = task_record(
            task_id="t_failed",
            body="failed",
            status="blocked",
            assignee="coder",
        )
        service.reader = type(
            "FailedTaskReader",
            (),
            {"task": staticmethod(lambda board, task_id: failed_task)},
        )()
        event = EventRecord(
            id=7,
            task_id="t_failed",
            run_id=3,
            kind="gave_up",
            payload={"reason": "attempt budget exhausted"},
            created_at=1,
        )

        service._enqueue_failure_limit(self.run, event)
        service._enqueue_failure_limit(self.run, event)

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event"], "failure-limit")

    def test_review_notification_is_idempotent_and_names_next_agent(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        service.config = config(self.root)
        metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            outcome="fail",
            issues=["acceptance rule is not testable"],
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
        )

        service._enqueue_review_failed(
            self.run, metadata, 3, WorkMode.FINALIZATION
        )
        service._enqueue_review_failed(
            self.run, metadata, 3, WorkMode.FINALIZATION
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload"])
        self.assertIn("next_agent=spec-writer", payload["text"])
        self.assertIn("finalization", payload["text"])
        self.assertIn(self.run.origin.initiator_open_id, payload["text"])

    def test_code_gate_notification_aggregates_both_roles_and_modification(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        service.config = config(self.root)
        test = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["dashboard browser assertion failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="fail",
            issues=["P&ID redraws the full canvas"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
        )

        service._enqueue_code_retry(self.run, test, review, 3)
        service._enqueue_code_retry(self.run, test, review, 3)

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload"])
        self.assertIn("tester=fail code-reviewer=fail", payload["text"])
        self.assertIn("modification=3/5", payload["text"])
        self.assertIn("[tester]", payload["text"])
        self.assertIn("[code-reviewer]", payload["text"])

    def test_skipped_test_notification_is_durable_and_idempotent(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        metadata = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            test_disposition="skipped_unavailable",
            skip_reason="browser runtime unavailable",
            verification=["unit tests passed", "browser preflight failed"],
            residual_risk=["browser flow remains unverified"],
        )

        service._enqueue_test_skipped(self.run, metadata)
        service._enqueue_test_skipped(self.run, metadata)

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload"])
        self.assertIn("结构化跳过", payload["text"])
        self.assertIn("browser runtime unavailable", payload["text"])
        self.assertIn("code-reviewer 将继续审查同一提交", payload["text"])

    def test_code_modification_count_excludes_initial_implementation(self) -> None:
        history: list[HistoryItem] = []
        parent = "t_root"
        for iteration in range(1, 7):
            card_id = f"t_implement_{iteration}"
            card = CardRecord(
                run=self.run,
                stage=Stage.IMPLEMENT,
                iteration=iteration,
                idempotency_key=(
                    f"{self.run.run_key}:implement:{iteration}:normal:work"
                ),
                parent_card_id=parent,
                assignee="coder",
                skills=["hollysys-implement", "glab"],
            )
            metadata = completion(
                self.root,
                Stage.IMPLEMENT,
                iteration=iteration,
                kanban_card_id=card_id,
            )
            task = task_record(
                task_id=card_id,
                body=render_card_body(card),
                status="done",
                assignee=card.assignee,
                idempotency_key=card.idempotency_key,
                tenant=self.run.run_key,
                skills=card.skills,
                parents=[parent],
                latest_metadata=metadata.model_dump(mode="json"),
            )
            managed = ManagedCard(
                board=self.run.workspace.board,
                card_id=card_id,
                run_key=self.run.run_key,
                stage=Stage.IMPLEMENT.value,
                iteration=iteration,
                idempotency_key=card.idempotency_key,
                parent_card_id=parent,
                purpose="work",
                created_at=iteration,
            )
            history.append(HistoryItem(managed, task))
            parent = card_id

        service = object.__new__(ControllerService)
        self.assertEqual(service._code_modification_count(history), 5)

    def test_completion_repository_evidence_must_match_run_base(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:implement:1:normal:work",
            parent_card_id="t_tasks",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
        )
        metadata = completion(
            self.root,
            Stage.IMPLEMENT,
            kanban_card_id="t_implement",
            repository_evidence={
                "repository_base_sha": "8" * 40,
                "inspected_paths": ["src/existing-module"],
                "existing_capabilities": ["existing MES framework"],
                "change_strategy": "modify_existing",
                "reuse_decisions": ["reuse existing service conventions"],
            },
        )
        task = task_record(
            task_id="t_implement",
            body=render_card_body(card),
            status="done",
            assignee=card.assignee,
            latest_metadata=metadata.model_dump(mode="json"),
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.IMPLEMENT.value,
            iteration=1,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        with self.assertRaisesRegex(
            ValueError, "not bound to the run base commit"
        ):
            service._validate_completion_context(
                self.run,
                HistoryItem(managed, task),
                CompletionMetadata.model_validate(
                    metadata.model_dump(mode="json")
                ),
            )

    def test_human_block_notification_is_durable_and_idempotent(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        task = task_record(
            task_id="t_blocked",
            body="blocked",
            status="blocked",
            assignee="tester",
            latest_outcome="blocked",
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.TEST.value,
            iteration=2,
            idempotency_key=f"{self.run.run_key}:test:2:normal:work",
            parent_card_id="t_implement",
            purpose="work",
            created_at=1,
        )
        comment = (
            "[human-block:v1]\n"
            f"block_id: {self.run.run_key}:t_blocked:7\n"
            "kind: permission\n"
            "summary: 测试环境无访问权限\n"
            "evidence: HTTP 403（凭据已脱敏）\n"
            "required_action: 为 tester 授予测试环境只读权限\n"
            "resume_check: 健康检查返回 200"
        )

        service._enqueue_human_block(
            self.run, HistoryItem(managed, task), comment
        )
        service._enqueue_human_block(
            self.run, HistoryItem(managed, task), comment
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event"], "human-block")
        payload = json.loads(pending[0]["payload"])
        self.assertIn("stage=test agent=tester card=t_blocked", payload["text"])
        self.assertIn("为 tester 授予测试环境只读权限", payload["text"])
        self.assertIn(self.run.origin.initiator_open_id, payload["text"])

    def test_business_ambiguity_block_is_rejected_and_released(self) -> None:
        root = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        card = CardRecord(
            run=self.run,
            stage=Stage.PLAN_WRITE,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:plan-write:1:normal:work",
            parent_card_id=root.id,
            assignee="planner",
            skills=["hollysys-write-plan", "glab"],
        )
        blocked = task_record(
            task_id="t_blocked",
            body=render_card_body(card),
            status="blocked",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[root.id],
            latest_outcome="blocked",
            comments=[
                {
                    "body": (
                        "[human-block:v1]\n"
                        f"block_id: {self.run.run_key}:t_blocked:8\n"
                        "kind: business_ambiguity\n"
                        "summary: PRD 有两种解释\n"
                        "evidence: 两条业务描述冲突\n"
                        "required_action: 请人类选择解释\n"
                        "resume_check: 人类已选择"
                    )
                }
            ],
        )
        root_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=root.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        blocked_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=blocked.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=root.id,
            purpose="work",
            created_at=2,
        )
        service = object.__new__(ControllerService)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: (
            [
                HistoryItem(root_managed, root),
                HistoryItem(blocked_managed, blocked),
            ],
            self.run,
        )
        service.gitlab = type(
            "NoMergeRequest",
            (),
            {"delivery_mr": staticmethod(lambda run: None)},
        )()
        comments: list[str] = []
        released: list[str] = []
        service.kanban = type(
            "RejectingKanban",
            (),
            {
                "comment": staticmethod(
                    lambda board, task_id, text, author: comments.append(text)
                ),
                "release": staticmethod(
                    lambda board, task_id: released.append(task_id)
                ),
            },
        )()

        service.reconcile_run(self.run.run_key)

        self.assertEqual(released, ["t_blocked"])
        self.assertIn("[controller-block-rejected:v3]", comments[0])

    def test_finalization_freezes_reconstructable_baseline(self) -> None:
        root_task = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        root_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=root_task.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        prior_reviews: list[HistoryItem] = []
        prior_parent = root_task.id
        for attempt in (1, 2):
            prior_id = f"t_review_{attempt}"
            prior_card = CardRecord(
                run=self.run,
                stage=Stage.SPEC_REVIEW,
                iteration=attempt,
                idempotency_key=(
                    f"{self.run.run_key}:spec-review:{attempt}:normal:work"
                ),
                parent_card_id=prior_parent,
                assignee="spec-reviewer",
                skills=["hollysys-review-spec", "glab"],
            )
            prior_metadata = completion(
                self.root,
                Stage.SPEC_REVIEW,
                iteration=attempt,
                outcome="fail",
                kanban_card_id=prior_id,
                issues=[f"review finding {attempt}"],
                artifact_paths=["docs/specs/feature/spec.md"],
                artifact_digest=f"{attempt}" * 64,
                artifact_commit_sha=f"{attempt}" * 40,
                gitlab_urls=[
                    (
                        "https://gitlab.example.com/group/project/"
                        f"-/merge_requests/2#note_{20 + attempt}"
                    )
                ],
            )
            prior_task = task_record(
                task_id=prior_id,
                body=render_card_body(prior_card),
                status="done",
                assignee=prior_card.assignee,
                idempotency_key=prior_card.idempotency_key,
                tenant=self.run.run_key,
                skills=prior_card.skills,
                parents=[prior_parent],
                latest_metadata=prior_metadata.model_dump(mode="json"),
            )
            prior_managed = ManagedCard(
                board=self.run.workspace.board,
                card_id=prior_id,
                run_key=self.run.run_key,
                stage=Stage.SPEC_REVIEW.value,
                iteration=attempt,
                idempotency_key=prior_card.idempotency_key,
                parent_card_id=prior_parent,
                purpose="work",
                created_at=attempt + 1,
            )
            prior_reviews.append(HistoryItem(prior_managed, prior_task))
            prior_parent = prior_id
        review_url = (
            "https://gitlab.example.com/group/project/-/merge_requests/2#note_31"
        )
        review_card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_REVIEW,
            iteration=3,
            idempotency_key=f"{self.run.run_key}:spec-review:3:normal:work",
            parent_card_id=prior_parent,
            assignee="spec-reviewer",
            skills=["hollysys-review-spec", "glab"],
        )
        review_metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            iteration=3,
            outcome="fail",
            kanban_card_id="t_review",
            issues=["PRD contains conflicting acceptance rules"],
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="a" * 64,
            artifact_commit_sha="b" * 40,
            gitlab_urls=[review_url],
        )
        review_task = task_record(
            task_id="t_review",
            body=render_card_body(review_card),
            status="done",
            assignee=review_card.assignee,
            idempotency_key=review_card.idempotency_key,
            tenant=self.run.run_key,
            skills=review_card.skills,
            parents=[prior_parent],
            latest_metadata=review_metadata.model_dump(mode="json"),
        )
        review_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=review_task.id,
            run_key=self.run.run_key,
            stage=Stage.SPEC_REVIEW.value,
            iteration=3,
            idempotency_key=review_card.idempotency_key,
            parent_card_id=prior_parent,
            purpose="work",
            created_at=2,
        )
        decision_url = (
            "https://gitlab.example.com/group/project/-/merge_requests/2#note_32"
        )
        repair_context = RepairContext(
            kind=RepairKind.REVIEW_FAILURE,
            trigger_card_id=review_task.id,
            issues=review_metadata.issues,
            review_attempt=3,
            review_limit=3,
        )
        final_card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_WRITE,
            iteration=4,
            mode=WorkMode.FINALIZATION,
            idempotency_key=f"{self.run.run_key}:spec-write:4:finalization:work",
            parent_card_id=review_task.id,
            assignee="spec-writer",
            skills=["hollysys-write-spec", "glab"],
            repair_context=repair_context,
        )
        final_metadata = completion(
            self.root,
            Stage.SPEC_WRITE,
            iteration=4,
            mode="finalization",
            kanban_card_id="t_final",
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="c" * 64,
            artifact_commit_sha="d" * 40,
            baseline_disposition="forced_after_review_limit",
            gitlab_urls=[decision_url],
            key_decisions=["Apply the safer acceptance rule"],
            residual_risk=["client behavior remains ambiguous"],
            forced_advance={
                "review_limit": 3,
                "final_review_card_id": review_task.id,
                "final_review_url": review_url,
                "decision_url": decision_url,
                "baseline_commit_sha": "d" * 40,
                "artifact_paths": ["docs/specs/feature/spec.md"],
                "artifact_digest": "c" * 64,
                "key_decisions": ["Apply the safer acceptance rule"],
                "unresolved_findings": ["conflicting PRD rules"],
                "residual_risks": ["client behavior remains ambiguous"],
            },
        )
        final_task = task_record(
            task_id="t_final",
            body=render_card_body(final_card),
            status="done",
            assignee=final_card.assignee,
            idempotency_key=final_card.idempotency_key,
            tenant=self.run.run_key,
            skills=final_card.skills,
            parents=[review_task.id],
            latest_metadata=final_metadata.model_dump(mode="json"),
        )
        final_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=final_task.id,
            run_key=self.run.run_key,
            stage=Stage.SPEC_WRITE.value,
            iteration=4,
            idempotency_key=final_card.idempotency_key,
            parent_card_id=review_task.id,
            purpose="work",
            created_at=3,
        )
        history = [
            HistoryItem(root_managed, root_task),
            *prior_reviews,
            HistoryItem(review_managed, review_task),
            HistoryItem(final_managed, final_task),
        ]
        service = object.__new__(ControllerService)
        service.config = config(self.root)

        service._validate_finalization_context(
            history, history[-1], final_metadata
        )
        baselines = service._frozen_baselines(history, self.run)

        self.assertEqual([item.phase for item in baselines], ["prd", "spec"])
        self.assertEqual(
            baselines[-1].disposition,
            BaselineDisposition.FORCED_AFTER_REVIEW_LIMIT,
        )
        self.assertEqual(
            baselines[-1].unresolved_findings,
            ["conflicting PRD rules"],
        )
        self.assertEqual(
            baselines[-1].key_decisions,
            ["Apply the safer acceptance rule"],
        )
        self.assertEqual(
            baselines[-1].residual_risk,
            ["client behavior remains ambiguous"],
        )

    def test_status_reports_phase_reviews_freezes_decisions_and_gates(self) -> None:
        root = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        root_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=root.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        gate_url = (
            "https://gitlab.example.com/group/project/-/merge_requests/2#note_10"
        )
        review_card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_REVIEW,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:spec-review:1:normal:work",
            parent_card_id=root.id,
            assignee="spec-reviewer",
            skills=["hollysys-review-spec", "glab"],
        )
        review_metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            artifact_paths=["docs/specs/feature/spec.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="reviewed",
            gitlab_urls=[gate_url],
            key_decisions=["Use repository compatibility defaults"],
        )
        review_task = task_record(
            task_id="t_abc",
            body=render_card_body(review_card),
            status="done",
            assignee=review_card.assignee,
            idempotency_key=review_card.idempotency_key,
            tenant=self.run.run_key,
            skills=review_card.skills,
            parents=[root.id],
            latest_metadata=review_metadata.model_dump(mode="json"),
        )
        review_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=review_task.id,
            run_key=self.run.run_key,
            stage=review_card.stage.value,
            iteration=review_card.iteration,
            idempotency_key=review_card.idempotency_key,
            parent_card_id=root.id,
            purpose="work",
            created_at=2,
        )
        plan_card = CardRecord(
            run=self.run,
            stage=Stage.PLAN_WRITE,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:plan-write:1:normal:work",
            parent_card_id=review_task.id,
            assignee="planner",
            skills=["hollysys-write-plan", "glab"],
        )
        plan_task = task_record(
            task_id="t_plan",
            body=render_card_body(plan_card),
            status="todo",
            assignee=plan_card.assignee,
            idempotency_key=plan_card.idempotency_key,
            tenant=self.run.run_key,
            skills=plan_card.skills,
            parents=[review_task.id],
        )
        plan_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=plan_task.id,
            run_key=self.run.run_key,
            stage=plan_card.stage.value,
            iteration=plan_card.iteration,
            idempotency_key=plan_card.idempotency_key,
            parent_card_id=review_task.id,
            purpose="work",
            created_at=3,
        )
        history = [
            HistoryItem(root_managed, root),
            HistoryItem(review_managed, review_task),
            HistoryItem(plan_managed, plan_task),
        ]
        service = object.__new__(ControllerService)
        service._lock = threading.RLock()
        service.config = config(self.root)
        service._run_protocol_version = lambda _: "hollysys-controller/v3"
        service._history = lambda _: (history, self.run)
        service.gitlab = type(
            "StatusGitLab",
            (),
            {
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {
                        "iid": 2,
                        "web_url": (
                            "https://gitlab.example.com/group/project/"
                            "-/merge_requests/2"
                        ),
                        "sha": "d" * 40,
                        "state": "opened",
                        "draft": True,
                    }
                ),
                "validate_gate": staticmethod(
                    lambda run, metadata: "id:11"
                ),
                "validate_artifact_gate_at_ref": staticmethod(
                    lambda run, metadata, ref: None
                ),
                "validate_baseline_at_ref": staticmethod(
                    lambda run, baseline, ref: None
                ),
            },
        )()

        status = service.status(self.run.run_key)

        self.assertEqual(status["phase"], "plan")
        self.assertEqual(status["stage"], "plan-write")
        self.assertEqual(status["active_card"]["agent"], "planner")
        self.assertEqual(status["review_attempts"]["spec"], 1)
        self.assertEqual(status["review_remaining"]["spec"], 2)
        self.assertEqual(
            status["frozen_artifacts"][1]["disposition"], "reviewed"
        )
        self.assertEqual(
            status["key_decisions"][0]["summary"],
            ["Use repository compatibility defaults"],
        )
        self.assertTrue(status["gates"]["spec-review"]["valid"])
        self.assertEqual(status["mr"]["head_sha"], "d" * 40)
        self.assertEqual(status["repository_base_sha"], "9" * 40)
        self.assertEqual(
            status["code_modifications"],
            {"used": 0, "remaining": 5, "limit": 5},
        )

        service.gitlab = type(
            "UnexpectedGitLab",
            (),
            {
                "__getattr__": lambda self, name: (_ for _ in ()).throw(
                    AssertionError(f"status-summary called GitLab: {name}")
                )
            },
        )()
        service.store = type(
            "SummaryStore",
            (),
            {
                "health": staticmethod(
                    lambda: {
                        "event_cursors": {self.run.workspace.board: 17},
                        "outbox_pending": 0,
                        "failed_operations": 0,
                    }
                )
            },
        )()
        service.reader = type(
            "SummaryReader",
            (),
            {"max_event_id": staticmethod(lambda board: 20)},
        )()

        summary = service.status_summary(self.run.run_key)

        self.assertEqual(summary["phase"], "plan")
        self.assertEqual(summary["stage"], "plan-write")
        self.assertEqual(summary["active_card"]["agent"], "planner")
        self.assertEqual(summary["snapshot"]["gitlab_audit"], "not_requested")
        self.assertEqual(summary["snapshot"]["event_lag"], 3)
        self.assertNotIn("mr", summary)
        self.assertNotIn("gates", summary)

    def test_frozen_violation_repairs_in_current_phase(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        service.config = config(self.root)
        created: list[tuple[Stage, WorkMode, RepairContext]] = []

        def capture(
            run,
            stage,
            parent_card_id,
            *,
            mode=WorkMode.NORMAL,
            repair_context=None,
        ):
            created.append((stage, mode, repair_context))
            return "created"

        service._create_work = capture
        result = service._create_frozen_repair(
            self.run,
            [],
            Phase.CODE,
            "t_review",
            "spec: frozen digest changed",
        )

        self.assertEqual(result, "created")
        self.assertEqual(created[0][0], Stage.IMPLEMENT)
        self.assertEqual(
            created[0][2].kind, RepairKind.FROZEN_ARTIFACT_VIOLATION
        )

    def test_protocol_failures_are_counted_separately_from_business_attempts(
        self,
    ) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_WRITE,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:spec-write:1:work",
            parent_card_id="t_root",
            assignee="spec-writer",
            skills=["hollysys-write-spec", "glab"],
        )
        task = task_record(
            task_id="t_invalid",
            body=render_card_body(card),
            status="done",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
            comments=[{"body": "[controller-protocol-error:v3]\nreason: bad metadata"}],
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        history = [HistoryItem(managed, task)]

        self.assertEqual(service._attempts_by_stage(history), {})
        self.assertEqual(
            service._protocol_failures_by_stage(history),
            {Stage.SPEC_WRITE: 1},
        )

    def test_validation_error_text_serializes_model_validator_context(self) -> None:
        review = completion(
            self.root,
            Stage.SPEC_REVIEW,
            outcome="fail",
            issues=["review finding"],
            artifact_paths=["docs/spec.md"],
            artifact_digest="a" * 64,
            artifact_commit_sha="b" * 40,
        ).model_dump(mode="json")
        review["repository_evidence"] = completion(
            self.root, Stage.SPEC_WRITE
        ).repository_evidence.model_dump(mode="json")

        with self.assertRaises(ValidationError) as caught:
            CompletionMetadata.model_validate(review)

        rendered = ControllerService._error_text(caught.exception)
        details = json.loads(rendered)
        self.assertEqual(details[0]["type"], "value_error")
        self.assertIn(
            "repository_evidence is only valid for an authoring pass",
            details[0]["msg"],
        )
        self.assertIsInstance(details[0]["ctx"]["error"], str)

    def test_cancelled_review_does_not_consume_review_limit(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_REVIEW,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:spec-review:1:normal:work",
            parent_card_id="t_root",
            assignee="spec-reviewer",
            skills=["hollysys-review-spec", "glab"],
        )
        metadata = completion(
            self.root,
            Stage.SPEC_REVIEW,
            outcome="cancelled",
            issues=["human block resumed"],
        )
        task = task_record(
            task_id="t_abc",
            body=render_card_body(card),
            status="done",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
            latest_metadata=metadata.model_dump(mode="json"),
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        history = [HistoryItem(managed, task)]

        self.assertEqual(
            service._attempts_by_stage(history), {Stage.SPEC_REVIEW: 1}
        )
        self.assertEqual(service._review_attempts_by_stage(history), {})

    def test_controller_failure_notification_uses_root_origin(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        root = task_record(
            task_id="t_root",
            body=render_run_body(self.run),
            status="done",
            idempotency_key=f"{self.run.run_key}:run-init",
            tenant=self.run.run_key,
        )
        service.store.add_managed_card(
            board=self.run.workspace.board,
            card_id=root.id,
            run_key=self.run.run_key,
            stage="run-init",
            iteration=0,
            idempotency_key=f"{self.run.run_key}:run-init",
            parent_card_id=None,
            purpose="root",
            created_at=1,
        )
        service.reader = type(
            "RootReader",
            (),
            {"task": staticmethod(lambda board, task_id: root)},
        )()

        service._enqueue_controller_failure(
            self.run.run_key, RuntimeError("GitLab unavailable")
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event"], "controller-failure")

    def test_human_abort_requires_confirmation_and_preserves_evidence(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:implement:1:normal:work",
            parent_card_id="t_root",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
        )
        task = task_record(
            task_id="t_work",
            body=render_card_body(card),
            status="running",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=card.stage.value,
            iteration=card.iteration,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        calls: list[tuple] = []
        service = object.__new__(ControllerService)
        service._lock = threading.RLock()
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "abort-controller.db")
        service._history = lambda _: ([HistoryItem(managed, task)], self.run)
        service.kanban = type(
            "AbortKanban",
            (),
            {
                "abort_task": staticmethod(
                    lambda board, task_id, reason: calls.append(
                        ("abort-task", board, task_id)
                    )
                )
            },
        )()
        service.gitlab = type(
            "AbortGitLab",
            (),
            {
                "abort_delivery": staticmethod(
                    lambda run, requested_by, reason: {
                        "state": "closed",
                        "web_url": "https://gitlab.example/mr/2",
                    }
                )
            },
        )()
        service.flush_outbox = lambda: calls.append(("flush",))
        service.last_reconcile_error = None

        requested = service.abort_request(
            {
                "run_key": self.run.run_key,
                "message_id": "om_abort_request",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
                "reason": "stop this delivery",
            }
        )
        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"],
            "active",
        )
        with service.store.connect() as conn:
            stored = conn.execute(
                "SELECT response FROM requests WHERE request_key=?",
                ("abort-request:om_abort_request",),
            ).fetchone()
        self.assertNotIn(requested["confirmation_token"], stored["response"])
        confirmed = service.abort_confirm(
            {
                "run_key": self.run.run_key,
                "token": requested["confirmation_token"],
                "message_id": "om_abort_confirm",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
            }
        )

        self.assertEqual(confirmed["state"], "aborted")
        self.assertEqual(
            calls[0],
            ("abort-task", self.run.workspace.board, task.id),
        )
        self.assertEqual(confirmed["continuation"], "finished")
        with service.store.connect() as conn:
            stored_confirm = conn.execute(
                "SELECT payload FROM requests WHERE request_key=?",
                ("abort-confirm:om_abort_confirm",),
            ).fetchone()
        self.assertNotIn(requested["confirmation_token"], stored_confirm["payload"])

    def test_verbose_level_notifies_agent_start_but_standard_does_not(self) -> None:
        task = task_record(
            task_id="t_work",
            body="body",
            status="running",
            assignee="coder",
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.IMPLEMENT.value,
            iteration=2,
            idempotency_key="work",
            parent_card_id="t_root",
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "notify-controller.db")
        service.config = config(self.root).model_copy(
            update={"notification_level": NotificationLevel.VERBOSE}
        )
        service._history = lambda _: ([], self.run)
        service.reader = type(
            "LifecycleReader",
            (),
            {"task": staticmethod(lambda board, card_id: task)},
        )()
        event = EventRecord(
            id=7,
            task_id=task.id,
            run_id=8,
            kind="claimed",
            payload={},
            created_at=9,
        )

        service._record_agent_lifecycle_event(managed, event)
        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertIn("Agent 已开始工作", pending[0]["payload"])

        service.config = service.config.model_copy(
            update={"notification_level": NotificationLevel.STANDARD}
        )
        service._record_agent_lifecycle_event(
            managed,
            EventRecord(
                id=10,
                task_id=task.id,
                run_id=11,
                kind="claimed",
                payload={},
                created_at=12,
            ),
        )
        self.assertEqual(len(service.store.pending_outbox()), 1)


if __name__ == "__main__":
    unittest.main()
