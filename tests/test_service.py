from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from hollysys_controller.errors import RunPolicyError
from hollysys_controller.gitlab import CheckedHeadConflict
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
    DeliveryBinding,
    NotificationLevel,
    Phase,
    RepairContext,
    RepairKind,
    Stage,
    StartRequest,
    WorkMode,
)
from hollysys_controller.service import ControllerService, HistoryItem
from hollysys_controller.store import ControllerStore, ManagedCard
from hollysys_controller.worker_recovery import SupervisorObservation
from tests.helpers import completion, config, run_record


def attach_test_store(
    service: ControllerService,
    root: Path,
    run,
    *,
    delivery: bool,
) -> None:
    if not hasattr(service, "store"):
        service.store = ControllerStore(root / "bound-controller.db")
    service.store.save_run(run)
    if not delivery:
        return
    binding = DeliveryBinding(
        mr_iid=2,
        mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
        creator="controller-bot",
        created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
        initial_head_sha="d" * 40,
        claim_note_id=99,
    )
    service.store.bind_delivery(run.run_key, binding)

    def validate_delivery_binding(current_run, current_binding):
        try:
            return service.gitlab.delivery_mr(
                current_run,
                current_binding.mr_iid,
            )
        except TypeError:
            return service.gitlab.delivery_mr(current_run)

    service.gitlab.validate_delivery_binding = validate_delivery_binding


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
    current_run_id: int | None = None,
    worker_pid: int | None = None,
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
        current_run_id=current_run_id,
        latest_summary=None,
        latest_metadata=latest_metadata,
        latest_outcome=latest_outcome,
        parents=parents or [],
        comments=comments or [],
        event_kinds=event_kinds or [],
        worker_pid=worker_pid,
    )


class ScratchDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = run_record(self.root)
        self.service = object.__new__(ControllerService)
        self.service.config = config(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record(self, scratch_dir: str) -> CardRecord:
        return CardRecord(
            run=self.run,
            stage=Stage.SPEC_REVIEW,
            iteration=1,
            idempotency_key=(
                f"{self.run.run_key}:spec-review:1:normal:work"
            ),
            parent_card_id="t_parent",
            assignee="spec-reviewer",
            skills=["hollysys-review-spec", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir=scratch_dir,
        )

    def test_attempt_scratch_directory_exists_before_dispatch(self) -> None:
        record = self._record(
            "/opt/data/scratch/0123456789abcdefghij/card-attempt"
        )

        actual = self.service._prepare_card_scratch_dir(record)

        expected = (
            self.root
            / "data"
            / "scratch"
            / "0123456789abcdefghij"
            / "card-attempt"
        )
        self.assertEqual(actual, expected)
        self.assertTrue(expected.is_dir())
        self.assertEqual(expected.stat().st_mode & 0o777, 0o700)

    def test_attempt_scratch_directory_rejects_symlink_component(self) -> None:
        scratch_root = self.root / "data" / "scratch"
        scratch_root.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (scratch_root / "0123456789abcdefghij").symlink_to(
            outside,
            target_is_directory=True,
        )
        record = self._record(
            "/opt/data/scratch/0123456789abcdefghij/card-attempt"
        )

        with self.assertRaisesRegex(
            RunPolicyError,
            "unsafe_scratch_component",
        ):
            self.service._prepare_card_scratch_dir(record)

    def test_resume_mode_skips_artifact_rework_after_verified_push(self) -> None:
        head = "d" * 40
        mode = self.service._resume_mode(
            stage=Stage.PLAN_WRITE,
            retry=True,
            workspace_state={
                "ok": True,
                "clean": True,
                "head_sha": head,
            },
            remote_head=head,
            expected_head="c" * 40,
        )

        self.assertEqual(mode, "protocol-finalization")
        self.assertEqual(
            self.service._resume_mode(
                stage=Stage.PLAN_REVIEW,
                retry=True,
                workspace_state={
                    "ok": True,
                    "clean": True,
                    "head_sha": head,
                },
                remote_head=head,
                expected_head=head,
            ),
            "review-resume",
        )
        self.assertEqual(
            self.service._resume_mode(
                stage=Stage.PLAN_WRITE,
                retry=True,
                workspace_state={
                    "ok": True,
                    "clean": False,
                    "head_sha": head,
                },
                remote_head=head,
                expected_head="c" * 40,
            ),
            "artifact-repair",
        )


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = run_record(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_controller_release_authorizes_only_one_initial_promotion(
        self,
    ) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.SPEC_WRITE,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:spec-write:1:normal:work",
            parent_card_id="t_root",
            assignee="spec-writer",
            skills=["hollysys-write-spec", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
        )
        task = task_record(
            task_id="t_spec",
            body=render_card_body(card),
            status="running",
            assignee=card.assignee,
            idempotency_key=card.idempotency_key,
            tenant=self.run.run_key,
            skills=card.skills,
            parents=[card.parent_card_id],
            event_kinds=["created", "promoted_manual"],
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
        item = HistoryItem(managed, task)
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "promotion-controller.db")

        self.assertFalse(service._promotion_is_authorized(item))

        operation_key = f"{card.idempotency_key}:release"
        payload = {"board": managed.board, "card_id": task.id}
        service.store.operation_result(operation_key, "release", payload)
        service.store.finish_operation(operation_key, {"card_id": task.id})

        self.assertTrue(service._promotion_is_authorized(item))
        duplicated = replace(
            task,
            event_kinds=[
                "created",
                "promoted_manual",
                "promoted_manual",
            ],
        )
        self.assertFalse(
            service._promotion_is_authorized(
                HistoryItem(managed, duplicated)
            )
        )

    def test_duplicate_start_returns_initialization_snapshot(self) -> None:
        cfg = config(self.root)
        store = ControllerStore(cfg.state_dir / "controller.db")
        service = ControllerService(cfg, store=store)
        raw = {
            "prd_blob_url": str(self.run.source.prd_blob_url),
            "prd_mr_url": str(self.run.source.prd_mr_url),
            "message_id": self.run.origin.message_id,
            "chat_id": self.run.origin.chat_id,
            "thread_id": self.run.origin.thread_id,
            "chat_type": self.run.origin.chat_type,
            "initiator": self.run.origin.initiator_open_id,
        }
        request = StartRequest.model_validate(raw)
        request_key = f"start:{request.message_id}"
        store.begin_request(
            request_key,
            "start",
            request.model_dump(mode="json"),
        )
        store.save_run(self.run)
        store.ensure_run_control(self.run.run_key)
        store.bind_request_run(request_key, self.run.run_key)
        service._active_requests.add(request_key)

        result = service.start(raw)

        self.assertEqual(result["request_status"], "running")
        self.assertEqual(result["run_key"], self.run.run_key)
        self.assertEqual(result["stage"], "run-initialization")
        self.assertIsNone(result["active_card"])

    def test_exception_does_not_reclaim_running_parent_without_supervisor(self) -> None:
        reason = "unauthorized state transition"
        suffix = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
        key = f"{self.run.run_key}:exception:{suffix}:work"
        parent = task_record(
            task_id="t_running",
            body="running",
            status="running",
            assignee="spec-writer",
        )
        exception = task_record(
            task_id="t_exception",
            body="exception",
            status="blocked",
            assignee="dispatcher",
            idempotency_key=key,
            tenant=self.run.run_key,
            skills=["hollysys-dispatch-kanban"],
            parents=[parent.id],
        )
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "exception-controller.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service.reader = type(
            "ParentReader",
            (),
            {"task": staticmethod(lambda board, task_id: parent)},
        )()
        calls: list[tuple[str, str]] = []
        service.kanban = type(
            "ExceptionKanban",
            (),
            {
                "abort_task": staticmethod(
                    lambda board, task_id, message: calls.append(
                        ("abort", task_id)
                    )
                ),
                "create_exception": staticmethod(
                    lambda run, parent_card_id, message, idempotency: (
                        calls.append(("create", parent_card_id))
                        or exception
                    )
                ),
            },
        )()

        with self.assertRaisesRegex(RunPolicyError, "worker_recovery_pending"):
            service._exception(self.run, parent.id, reason)

        self.assertEqual(calls, [])
        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"],
            "active",
        )

    def test_resolve_reuses_retry_created_before_request_commit(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=2,
            idempotency_key=f"{self.run.run_key}:implement:2:work",
            parent_card_id="t_blocked",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            "gate_phase: implementation_entry\n"
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
        service._history = lambda _: ([HistoryItem(managed, root)], self.run)
        service.gitlab = type(
            "NoMergeRequest",
            (),
            {"delivery_mr": staticmethod(lambda run: None)},
        )()
        attach_test_store(service, self.root, self.run, delivery=False)
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
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
        attach_test_store(service, self.root, self.run, delivery=False)
        released: list[str] = []
        service._ensure_work_published = (
            lambda run, task: released.append(task.id)
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(released, ["t_held"])

    def test_reconcile_test_failure_returns_to_coder_without_review(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.TEST,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:test:1:normal:work",
            parent_card_id="t_implement",
            assignee="tester",
            skills=["hollysys-test", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
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
        attach_test_store(service, self.root, self.run, delivery=True)
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

        self.assertEqual(created[0][0], Stage.IMPLEMENT)
        repair = created[0][1]
        self.assertIsNotNone(repair)
        self.assertEqual(repair.kind, RepairKind.CODE_GATE_FAILURE)
        self.assertEqual(repair.related_card_ids, ["t_test"])
        self.assertIn("[tester] browser assertion failed", repair.issues)

    def test_reconcile_implement_pass_keeps_mr_draft_and_creates_test(
        self,
    ) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=2,
            idempotency_key=f"{self.run.run_key}:implement:2:normal:work",
            parent_card_id="t_code_review",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
        )
        metadata = completion(
            self.root,
            Stage.IMPLEMENT,
            iteration=2,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_implement_2",
        )
        task = task_record(
            task_id="t_implement_2",
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
            stage=Stage.IMPLEMENT.value,
            iteration=2,
            idempotency_key=card.idempotency_key,
            parent_card_id=card.parent_card_id,
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
        service._history = lambda _: ([HistoryItem(managed, task)], self.run)
        service.gitlab = type(
            "DraftDelivery",
            (),
            {
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {
                        "iid": 2,
                        "sha": "d" * 40,
                        "draft": True,
                    }
                ),
                "validate_repository_evidence": staticmethod(
                    lambda run, current: None
                ),
                "validate_author_completion": staticmethod(
                    lambda run, current: {"iid": 2, "sha": "d" * 40}
                ),
                "mark_delivery_ready": staticmethod(
                    lambda run, binding: (_ for _ in ()).throw(
                        AssertionError("IMPLEMENT pass changed Draft state")
                    )
                ),
            },
        )()
        attach_test_store(service, self.root, self.run, delivery=True)
        service._frozen_violation = lambda run, history, ref: None
        service._review_attempts_by_stage = lambda history: {}
        service._code_modification_count = lambda history: 1
        service._enqueue_agent_completed = lambda *args, **kwargs: None
        created: list[Stage] = []
        service._create_work = (
            lambda run, stage, parent, **kwargs: created.append(stage)
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(created, [Stage.TEST])

    def test_reconcile_code_review_aggregates_both_gate_findings(self) -> None:
        test_card = CardRecord(
            run=self.run,
            stage=Stage.TEST,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:test:1:normal:work",
            parent_card_id="t_implement",
            assignee="tester",
            skills=["hollysys-test", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
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
        attach_test_store(service, self.root, self.run, delivery=True)
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
            gitlab_urls=[
                (
                    "https://gitlab.example.com/group/project/"
                    "-/merge_requests/2#note_31"
                )
            ],
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
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("**下一位 Agent：** SPEC Writer", payload["content"])
        self.assertIn("**审查轮次：** 3/3", payload["content"])
        self.assertIn("第 3/3 轮审查未通过", payload["content"])
        self.assertIn("强制收敛", payload["content"])
        self.assertIn(
            "[查看 MR !2 审查记录]",
            payload["content"],
        )
        self.assertNotIn("证据 1", payload["content"])
        self.assertIn(self.run.origin.initiator_open_id, payload["content"])

    def test_agent_completion_uses_document_write_review_round(
        self,
    ) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        service.config = config(self.root)
        task = task_record(
            task_id="t_61010b84",
            body="completed",
            status="done",
            assignee="tasker",
        )
        task = replace(task, created_at=1, completed_at=535)
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.TASKS_WRITE.value,
            iteration=2,
            idempotency_key="tasks-write-2",
            parent_card_id="t_parent",
            purpose="work",
            created_at=1,
        )
        metadata = completion(
            self.root,
            Stage.TASKS_WRITE,
            outcome="pass",
            iteration=2,
        )

        service._enqueue_agent_completed(
            self.run,
            HistoryItem(managed, task),
            metadata,
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        payload = json.loads(pending[0]["payload"])
        content = payload["content"]
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("**✅ Tasker Agent 工作已完成**", content)
        self.assertIn(
            f"**任务 ID：** `{self.run.run_key}`",
            content,
        )
        self.assertIn("**阶段：** tasks-write（拆分 TASKS）", content)
        self.assertIn("**阶段轮次：** 2/3", content)
        self.assertNotIn("**执行尝试：**", content)
        self.assertIn("**Agent：** Tasker", content)
        self.assertNotIn("**Agent：** Tester", content)
        self.assertIn("**Card：** `t_61010b84`", content)
        self.assertIn("**结论：** pass（通过）", content)
        self.assertIn("**耗时：** 8分54秒", content)

    def test_document_round_counts_each_write_review_pair_once(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)

        def reviewed(iteration: int, outcome: str) -> HistoryItem:
            metadata = completion(
                self.root,
                Stage.PLAN_REVIEW,
                iteration=iteration,
                outcome=outcome,
                issues=(["需要修订"] if outcome == "fail" else []),
                artifact_paths=["docs/plans/feature/plan.md"],
                artifact_digest="b" * 64,
                artifact_commit_sha="c" * 40,
                baseline_disposition=(
                    "reviewed" if outcome == "pass" else None
                ),
            )
            task = task_record(
                task_id=f"t_plan_review_{iteration}",
                body="review",
                status="done",
                assignee="plan-reviewer",
                latest_metadata=metadata.model_dump(mode="json"),
            )
            managed = ManagedCard(
                board=self.run.workspace.board,
                card_id=task.id,
                run_key=self.run.run_key,
                stage=Stage.PLAN_REVIEW.value,
                iteration=iteration,
                idempotency_key=f"plan-review-{iteration}",
                parent_card_id="t_parent",
                purpose="work",
                created_at=iteration,
            )
            return HistoryItem(managed, task)

        first_pair = [reviewed(1, "fail")]
        self.assertEqual(
            service._document_round(
                first_pair,
                Stage.PLAN_WRITE,
                accepted_completion=False,
            ),
            2,
        )
        self.assertEqual(
            service._document_round(
                first_pair,
                Stage.PLAN_REVIEW,
                accepted_completion=False,
            ),
            2,
        )

        second_pair = [*first_pair, reviewed(2, "pass")]
        self.assertEqual(
            service._document_round(
                second_pair,
                Stage.PLAN_REVIEW,
                accepted_completion=True,
            ),
            2,
        )

    def test_phase_frozen_is_concise_and_uses_meaningful_links(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
        service.config = config(self.root)
        metadata = completion(
            self.root,
            Stage.PLAN_REVIEW,
            iteration=2,
            outcome="pass",
            artifact_paths=["docs/plans/feature/plan.md"],
            artifact_digest="b" * 64,
            artifact_commit_sha="c" * 40,
            baseline_disposition="reviewed",
            gitlab_urls=[
                (
                    "https://gitlab.example.com/group/project/"
                    "-/merge_requests/2#note_32"
                )
            ],
            key_decisions=[
                "沿用现有 APS 能力并做局部扩展；这是一段故意拉长的说明，"
                "用于确认飞书摘要不会把长段审查过程完整倾倒给人类。" * 4
            ],
            residual_risk=["目标环境仍需回读权限动作配置。"],
        )

        service._enqueue_phase_frozen(
            self.run,
            Phase.PLAN,
            metadata,
            2,
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        content = json.loads(pending[0]["payload"])["content"]
        self.assertIn("PLAN 第 2/3 轮审查通过，工件已冻结", content)
        self.assertIn("**审查轮次：** 2/3", content)
        self.assertIn("[查看 MR !2]", content)
        self.assertIn("[查看 MR !2 审查记录]", content)
        self.assertIn("…", content)
        self.assertNotIn("证据 1", content)

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
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("**Tester：** fail（未通过）", payload["content"])
        self.assertIn(
            "**Code Reviewer：** fail（未通过）",
            payload["content"],
        )
        self.assertIn("**修改轮次：** 3/5", payload["content"])
        self.assertIn(
            "[查看 MR !2 详情](https://gitlab.example.com/group/project/-/merge_requests/2)",
            payload["content"],
        )
        self.assertIn("dashboard browser assertion failed", payload["content"])
        self.assertIn("P&amp;ID redraws the full canvas", payload["content"])

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
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("结构化跳过", payload["content"])
        self.assertIn("browser runtime unavailable", payload["content"])
        self.assertIn(
            "Code Reviewer 将继续审查同一提交",
            payload["content"],
        )
        self.assertIn(
            "[!2](https://gitlab.example.com/group/project/-/merge_requests/2)",
            payload["content"],
        )

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
                context_digest="e" * 64,
                expected_head_sha=self.run.workspace.repository_base_sha,
                scratch_dir="/opt/data/scratch/test-attempt",
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
        service.store = type(
            "AcceptedRuntimeStore",
            (),
            {
                "card_runtime": staticmethod(
                    lambda board, card_id: {
                        "attempt_status": "completed_accepted"
                    }
                )
            },
        )()
        service._validate_completion_identity = lambda *args: None
        service._validate_completion_context = staticmethod(
            lambda *args: (_ for _ in ()).throw(
                AssertionError("historical count performed live validation")
            )
        )
        self.assertEqual(service._code_modification_count(history), 5)

    def test_code_modification_count_excludes_rejected_completion(self) -> None:
        history: list[HistoryItem] = []
        parent = "t_root"
        for iteration in range(1, 4):
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
                context_digest="e" * 64,
                expected_head_sha=self.run.workspace.repository_base_sha,
                scratch_dir="/opt/data/scratch/test-attempt",
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
        service.store = type(
            "MixedRuntimeStore",
            (),
            {
                "card_runtime": staticmethod(
                    lambda board, card_id: {
                        "attempt_status": (
                            "completed_rejected"
                            if card_id == "t_implement_2"
                            else "completed_accepted"
                        )
                    }
                )
            },
        )()
        service._validate_completion_identity = lambda *args: None

        self.assertEqual(service._code_modification_count(history), 1)

    def test_delivery_ready_operation_is_bound_to_checked_head(self) -> None:
        service = object.__new__(ControllerService)
        service._run_control = lambda run_key: {"state_version": 7}
        calls: list[dict] = []

        def operation(key, kind, payload, action, **kwargs):
            calls.append(
                {
                    "key": key,
                    "kind": kind,
                    "payload": payload,
                    **kwargs,
                }
            )
            return {"draft": False}

        service._operation = operation
        service.gitlab = type(
            "ReadyGitLab",
            (),
            {"mark_delivery_ready": staticmethod(lambda run, binding: {})},
        )()
        binding = DeliveryBinding(
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            creator="controller-bot",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            initial_head_sha="a" * 40,
            claim_note_id=99,
        )
        head = "d" * 40

        service._mark_delivery_ready_at_head(self.run, binding, head)

        self.assertEqual(
            calls,
            [
                {
                    "key": f"{self.run.run_key}:delivery-ready:{head}",
                    "kind": "delivery-ready",
                    "payload": {
                        "run_key": self.run.run_key,
                        "mr_iid": 2,
                        "checked_head": head,
                    },
                    "run_key": self.run.run_key,
                    "expected_state_version": 7,
                    "expected_head_sha": head,
                }
            ],
        )

    def test_delivery_ready_rejects_changed_head_before_update(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "ready-controller.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service._run_control = service.store.run_control
        binding = DeliveryBinding(
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            creator="controller-bot",
            created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
            initial_head_sha="a" * 40,
            claim_note_id=99,
        )
        service.gitlab = type(
            "ChangedHead",
            (),
            {
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {
                        "iid": 2,
                        "sha": "e" * 40,
                        "draft": True,
                    }
                ),
                "mark_delivery_ready": staticmethod(
                    lambda run, current: (_ for _ in ()).throw(
                        AssertionError("changed head was marked ready")
                    )
                ),
            },
        )()

        with self.assertRaises(CheckedHeadConflict):
            service._mark_delivery_ready_at_head(self.run, binding, "d" * 40)

    def test_code_review_pass_finishes_ready_without_merge(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.gitlab = type("BindingStub", (), {})()
        attach_test_store(service, self.root, self.run, delivery=True)
        service.store.ensure_run_control(self.run.run_key)
        test = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_test",
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_review",
        )
        latest = HistoryItem(
            ManagedCard(
                board=self.run.workspace.board,
                card_id="t_review",
                run_key=self.run.run_key,
                stage=Stage.CODE_REVIEW.value,
                iteration=1,
                idempotency_key="review",
                parent_card_id="t_test",
                purpose="work",
                created_at=1,
            ),
            task_record(task_id="t_review", body="review", status="done"),
        )
        live = {
            "iid": 2,
            "sha": "d" * 40,
            "draft": False,
            "web_url": "https://gitlab.example.com/group/project/-/merge_requests/2",
        }
        service.gitlab = type(
            "ReadyMR",
            (),
            {"delivery_mr": staticmethod(lambda run, mr_iid=None: live)},
        )()
        ready_calls: list[str] = []
        service._mark_delivery_ready_at_head = (
            lambda run, binding, head: ready_calls.append(head) or live
        )
        service._frozen_violation = lambda run, history, head: None
        notifications: list[str] = []
        service._enqueue_code_flow_completed = (
            lambda run, history, **kwargs: notifications.append(
                kwargs["terminal_state"]
            )
        )

        service._finalize_code_flow(
            self.run,
            [latest],
            latest,
            review,
            terminal_state="completed_ready",
            paired_test=test,
            code_modifications=2,
        )

        self.assertEqual(ready_calls, ["d" * 40])
        self.assertEqual(notifications, ["completed_ready"])
        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"],
            "completed_ready",
        )

    def test_fifth_failed_test_finishes_without_ready_or_review(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.gitlab = type("BindingStub", (), {})()
        attach_test_store(service, self.root, self.run, delivery=True)
        service.store.ensure_run_control(self.run.run_key)
        test = completion(
            self.root,
            Stage.TEST,
            outcome="fail",
            issues=["integration suite failed"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_test",
        )
        latest = HistoryItem(
            ManagedCard(
                board=self.run.workspace.board,
                card_id="t_test",
                run_key=self.run.run_key,
                stage=Stage.TEST.value,
                iteration=1,
                idempotency_key="test",
                parent_card_id="t_implement",
                purpose="work",
                created_at=1,
            ),
            task_record(task_id="t_test", body="test", status="done"),
        )
        live = {"iid": 2, "sha": "d" * 40, "draft": True}
        service.gitlab = type(
            "DraftMR",
            (),
            {"delivery_mr": staticmethod(lambda run, mr_iid=None: live)},
        )()
        service._mark_delivery_ready_at_head = lambda *args: (_ for _ in ()).throw(
            AssertionError("failed test changed MR readiness")
        )
        service._frozen_violation = lambda run, history, head: None
        service._enqueue_code_flow_completed = lambda *args, **kwargs: None

        service._finalize_code_flow(
            self.run,
            [latest],
            latest,
            test,
            terminal_state="completed_test_failed",
            paired_test=None,
            code_modifications=5,
        )

        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"],
            "completed_test_failed",
        )

    def test_legacy_exhausted_exception_is_reopened_for_terminalization(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "legacy-controller.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service.store.set_run_exception(
            self.run.run_key,
            "code modification limit 5 exhausted; head=" + "d" * 40,
        )
        test = completion(
            self.root,
            Stage.TEST,
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_test",
        )
        review = completion(
            self.root,
            Stage.CODE_REVIEW,
            outcome="fail",
            issues=["review defect remains"],
            mr_iid=2,
            mr_url="https://gitlab.example.com/group/project/-/merge_requests/2",
            head_sha="d" * 40,
            kanban_card_id="t_review",
        )
        test_item = HistoryItem(
            ManagedCard(
                board=self.run.workspace.board,
                card_id="t_test",
                run_key=self.run.run_key,
                stage=Stage.TEST.value,
                iteration=1,
                idempotency_key="test",
                parent_card_id="t_implement",
                purpose="work",
                created_at=1,
            ),
            task_record(
                task_id="t_test",
                body="test",
                status="done",
                latest_metadata=test.model_dump(mode="json"),
            ),
        )
        review_item = HistoryItem(
            ManagedCard(
                board=self.run.workspace.board,
                card_id="t_review",
                run_key=self.run.run_key,
                stage=Stage.CODE_REVIEW.value,
                iteration=1,
                idempotency_key="review",
                parent_card_id="t_test",
                purpose="work",
                created_at=2,
            ),
            task_record(
                task_id="t_review",
                body="review",
                status="done",
                latest_metadata=review.model_dump(mode="json"),
            ),
        )
        exception_item = HistoryItem(
            ManagedCard(
                board=self.run.workspace.board,
                card_id="t_exception",
                run_key=self.run.run_key,
                stage="exception",
                iteration=1,
                idempotency_key="exception",
                parent_card_id="t_review",
                purpose="exception",
                created_at=3,
            ),
            task_record(
                task_id="t_exception",
                body="exception",
                status="blocked",
            ),
        )
        history = [test_item, review_item, exception_item]
        service._history = lambda run_key: (history, self.run)
        service.reader = type(
            "LegacyReader",
            (),
            {"task": staticmethod(lambda board, task_id: exception_item.task)},
        )()
        service._code_modification_count = lambda current: 5
        calls: list[tuple[str, str]] = []
        service.kanban = type(
            "LegacyKanban",
            (),
            {
                "comment": staticmethod(
                    lambda board, card, text, author: calls.append(
                        ("comment", card)
                    )
                ),
                "abort_task": staticmethod(
                    lambda board, card, reason: calls.append(("abort", card))
                ),
            },
        )()

        changed = service._upgrade_legacy_exhausted_code_exception(
            self.run.run_key
        )

        self.assertTrue(changed)
        self.assertEqual(calls, [("comment", "t_exception"), ("abort", "t_exception")])
        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"], "active"
        )

    def test_completion_repository_evidence_must_match_run_base(self) -> None:
        card = CardRecord(
            run=self.run,
            stage=Stage.IMPLEMENT,
            iteration=1,
            idempotency_key=f"{self.run.run_key}:implement:1:normal:work",
            parent_card_id="t_tasks",
            assignee="coder",
            skills=["hollysys-implement", "glab"],
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service.gitlab = type(
            "CurrentHead",
            (),
            {
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: {"iid": 2, "sha": "d" * 40}
                )
            },
        )()
        attach_test_store(service, self.root, self.run, delivery=True)
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
        self.assertEqual(payload["format"], "markdown")
        self.assertIn("**阶段：** test（测试）", payload["content"])
        self.assertIn("**Agent：** Tester", payload["content"])
        self.assertIn("**Card：** `t_blocked`", payload["content"])
        self.assertIn("为 tester 授予测试环境只读权限", payload["content"])
        self.assertIn(self.run.origin.initiator_open_id, payload["content"])

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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
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
        attach_test_store(service, self.root, self.run, delivery=False)
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
        exceptions: list[str] = []
        service._exception = lambda run, card_id, reason: exceptions.append(
            reason
        )

        service.reconcile_run(self.run.run_key)

        self.assertEqual(released, [])
        self.assertIn("[controller-block-rejected:v4]", comments[0])
        self.assertIn("unsupported human block", exceptions[0])

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
                context_digest="e" * 64,
                expected_head_sha=self.run.workspace.repository_base_sha,
                scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._validate_completion_context = lambda *args: None

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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
        service._run_protocol_version = lambda _: "hollysys-controller/v4"
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
        attach_test_store(service, self.root, self.run, delivery=True)

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
        active_history = history
        history = history[:2]
        idle_status = service.status(self.run.run_key)
        self.assertEqual(idle_status["phase"], "active")
        self.assertEqual(idle_status["stage"], "active")
        self.assertNotIn("reconciling", json.dumps(idle_status))
        history = active_history

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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            comments=[{"body": "[controller-protocol-error:v4]\nreason: bad metadata"}],
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            context_digest="e" * 64,
            expected_head_sha=self.run.workspace.repository_base_sha,
            scratch_dir="/opt/data/scratch/test-attempt",
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
            current_run_id=4,
            worker_pid=404,
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
        service.reader = type(
            "AbortReader",
            (),
            {"task": staticmethod(lambda board, task_id: task)},
        )()
        service.worker_recovery = type(
            "ConfirmedExitRecovery",
            (),
            {
                "observe": staticmethod(
                    lambda identity, terminate_running: SupervisorObservation(
                        "terminated", 10, identity.worker_pid
                    )
                )
            },
        )()
        service.kanban = type(
            "AbortKanban",
            (),
            {
                "abort_task": staticmethod(
                    lambda board, task_id, reason, **kwargs: calls.append(
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
                    lambda run, mr_iid, requested_by, reason: {
                        "state": "closed",
                        "web_url": "https://gitlab.example/mr/2",
                    }
                )
            },
        )()
        attach_test_store(service, self.root, self.run, delivery=True)
        service.store.add_managed_card(
            board=managed.board,
            card_id=managed.card_id,
            run_key=managed.run_key,
            stage=managed.stage,
            iteration=managed.iteration,
            idempotency_key=managed.idempotency_key,
            parent_card_id=managed.parent_card_id,
            purpose=managed.purpose,
            created_at=managed.created_at,
        )
        service.store.register_card_attempt(
            board=managed.board,
            card_id=managed.card_id,
            profile="coder",
            dispatch_key=managed.idempotency_key,
            worktree=self.run.workspace.worktree,
            branch=self.run.workspace.branch,
        )
        service.store.record_card_runtime_event(
            board=managed.board,
            card_id=managed.card_id,
            kind="worker_started",
            created_at=1,
            worker_session_id="kanban-run:4",
            worker_pid=404,
            lease_seconds=300,
            run_id=f"{managed.board}:4",
        )
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
        replayed = service.abort_request(
            {
                "run_key": self.run.run_key,
                "message_id": "om_abort_request",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
                "reason": "stop this delivery",
            }
        )
        self.assertEqual(replayed["error_code"], "token_unavailable")
        self.assertTrue(replayed["reissue_required"])
        self.assertNotIn("confirmation_token", replayed)
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

        self.assertEqual(confirmed["state"], "abort_requested")
        self.assertEqual(confirmed["continuation"], "pending-reconcile")
        self.assertEqual(calls, [])
        service._continue_abort(self.run.run_key)
        self.assertEqual(
            service.store.run_control(self.run.run_key)["state"],
            "aborted",
        )
        self.assertEqual(
            calls[0],
            ("abort-task", self.run.workspace.board, task.id),
        )
        with service.store.connect() as conn:
            stored_confirm = conn.execute(
                "SELECT payload FROM requests WHERE request_key=?",
                ("abort-confirm:om_abort_confirm",),
            ).fetchone()
        self.assertNotIn(requested["confirmation_token"], stored_confirm["payload"])

    def test_restart_closes_sensitive_requests_without_replaying_tokens(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "sensitive-restart.db")
        service.store.ensure_run_control(self.run.run_key)
        abort_request_key = "abort-request:om_interrupted"
        service.store.begin_request(
            abort_request_key,
            "abort-request",
            {
                "run_key": self.run.run_key,
                "message_id": "om_interrupted",
            },
        )

        service._recover_sensitive_request(
            service.store.running_requests()[0],
        )

        unavailable = service.store.begin_request(
            abort_request_key,
            "abort-request",
            {
                "run_key": self.run.run_key,
                "message_id": "om_interrupted",
            },
        )
        self.assertEqual(unavailable["error_code"], "token_unavailable")
        self.assertTrue(unavailable["reissue_required"])

        confirm_key = "abort-confirm:om_committed"
        confirm_payload = {
            "run_key": self.run.run_key,
            "message_id": "om_committed",
            "token_hash": "a" * 64,
        }
        service.store.begin_request(
            confirm_key,
            "abort-confirm",
            confirm_payload,
        )
        with service.store.connect() as conn:
            conn.execute(
                """
                UPDATE run_control
                SET state='abort_requested', state_version=state_version+1,
                    abort_requested_by=?, abort_reason=?,
                    abort_requested_at=?, updated_at=?
                WHERE run_key=?
                """,
                (
                    self.run.origin.initiator_open_id,
                    "stop",
                    1,
                    1,
                    self.run.run_key,
                ),
            )

        committed = next(
            request
            for request in service.store.running_requests()
            if request["request_key"] == confirm_key
        )
        service._recover_sensitive_request(committed)

        recovered = service.store.begin_request(
            confirm_key,
            "abort-confirm",
            confirm_payload,
        )
        self.assertEqual(recovered["state"], "abort_requested")
        self.assertEqual(recovered["continuation"], "pending-reconcile")

    def test_exception_recovery_is_authorized_and_versioned(self) -> None:
        exception = task_record(
            task_id="t_exception",
            body="exception evidence",
            status="blocked",
            assignee="dispatcher",
            idempotency_key="exception-key",
            tenant=self.run.run_key,
            skills=["hollysys-dispatch-kanban"],
            parents=["t_parent"],
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=exception.id,
            run_key=self.run.run_key,
            stage="exception",
            iteration=1,
            idempotency_key="exception-key",
            parent_card_id="t_parent",
            purpose="exception",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "recover-controller.db")
        service.store.ensure_run_control(self.run.run_key)
        service.store.set_run_exception(self.run.run_key, "pipeline skipped")
        service._history = lambda _: ([HistoryItem(managed, exception)], self.run)
        service.reader = type(
            "RecoveryReader",
            (),
            {"task": staticmethod(lambda board, task_id: exception)},
        )()
        calls: list[tuple] = []
        service.kanban = type(
            "RecoveryKanban",
            (),
            {
                "abort_task": staticmethod(
                    lambda board, task_id, reason: calls.append(
                        ("archive", board, task_id)
                    )
                )
            },
        )()
        recovered = service.recover(
            {
                "run_key": self.run.run_key,
                "message_id": "om_recover",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
                "reason": "pipeline policy fixed and verified",
            }
        )

        self.assertEqual(recovered["state"], "active")
        self.assertEqual(recovered["continuation"], "pending-reconcile")
        self.assertEqual(
            calls,
            [
                ("archive", self.run.workspace.board, exception.id),
            ],
        )

    def test_pre_root_exception_is_visible_and_human_recoverable(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "pre-root-controller.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service.store.operation_result(
            f"{self.run.run_key}:board",
            "board",
            {
                "board": self.run.workspace.board,
                "name": self.run.project.project_display_name,
                "worktree": self.run.workspace.worktree,
            },
        )
        service.store.mark_operation_uncertain(
            f"{self.run.run_key}:board",
            "profile override failed: permission denied",
        )
        service.store.set_run_exception(
            self.run.run_key,
            f"unknown run {self.run.run_key}",
        )

        summary = service.status_summary(self.run.run_key)

        self.assertEqual(summary["phase"], "exception")
        self.assertEqual(summary["stage"], "run-initialization")
        self.assertIsNone(summary["active_card"])
        self.assertEqual(
            summary["initialization"]["operations"][0]["status"],
            "uncertain",
        )
        self.assertIn("permission denied", summary["blocked"])
        self.assertNotIn("unknown run", summary["blocked"])

        service._initialize_run = lambda run, base_sha: {
            "stage": Stage.SPEC_WRITE.value,
            "active_card": "t_spec",
        }
        recovered = service.recover(
            {
                "run_key": self.run.run_key,
                "message_id": "om_recover_pre_root",
                "sender": self.run.origin.initiator_open_id,
                "chat_id": self.run.origin.chat_id,
                "thread_id": self.run.origin.thread_id,
                "reason": "container UID and profile access verified",
            }
        )

        self.assertEqual(recovered["state"], "active")
        self.assertEqual(recovered["continuation"], "initialization-resumed")
        self.assertEqual(recovered["active_card"], "t_spec")

    def test_pre_root_controller_failure_is_not_silently_dropped(self) -> None:
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "pre-root-notify.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)

        service._enqueue_controller_failure(
            self.run.run_key,
            PermissionError("profile override failed"),
        )

        pending = service.store.pending_outbox(limit=10)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["event"], "controller-failure")

    def _watchdog_threshold_fixture(
        self,
        name: str,
        *,
        started_at: int,
    ) -> tuple[ControllerService, TaskRecord, ManagedCard]:
        task = task_record(
            task_id=f"t_{name}",
            body="worker body",
            status="running",
            assignee="coder",
            idempotency_key=name,
            tenant=self.run.run_key,
            current_run_id=4,
            worker_pid=404,
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.IMPLEMENT.value,
            iteration=1,
            idempotency_key=name,
            parent_card_id="t_parent",
            purpose="work",
            created_at=started_at,
        )
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / f"{name}.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service.store.add_managed_card(
            board=managed.board,
            card_id=managed.card_id,
            run_key=managed.run_key,
            stage=managed.stage,
            iteration=managed.iteration,
            idempotency_key=managed.idempotency_key,
            parent_card_id=managed.parent_card_id,
            purpose=managed.purpose,
            created_at=managed.created_at,
        )
        service.store.register_card_attempt(
            board=managed.board,
            card_id=managed.card_id,
            profile="coder",
            dispatch_key=managed.idempotency_key,
            worktree=self.run.workspace.worktree,
            branch=self.run.workspace.branch,
        )
        service.store.record_card_runtime_event(
            board=managed.board,
            card_id=managed.card_id,
            kind="worker_started",
            created_at=started_at,
            worker_session_id="kanban-run:4",
            worker_pid=404,
            lease_seconds=service.config.worker_progress_lease_seconds,
            run_id=f"{managed.board}:4",
        )
        service.reader = type(
            "ThresholdReader",
            (),
            {"task": staticmethod(lambda board, task_id: task)},
        )()
        service._history = lambda _: ([HistoryItem(managed, task)], self.run)
        return service, task, managed

    def test_watchdog_never_probes_when_heartbeat_is_fresh(self) -> None:
        now = int(time.time())
        service, task, managed = self._watchdog_threshold_fixture(
            "fresh_heartbeat",
            started_at=now - 1900,
        )
        service.store.record_card_runtime_event(
            board=managed.board,
            card_id=managed.card_id,
            kind="heartbeat",
            created_at=now,
            worker_session_id="kanban-run:4",
            worker_pid=404,
            lease_seconds=service.config.worker_progress_lease_seconds,
            run_id=f"{managed.board}:4",
        )
        service.worker_recovery = type(
            "ForbiddenRecovery",
            (),
            {
                "observe": staticmethod(
                    lambda identity, terminate_running: (_ for _ in ()).throw(
                        AssertionError("fresh heartbeat must not probe")
                    )
                )
            },
        )()

        service._enqueue_stale_worker_notices()

        runtime = service.store.card_runtime(managed.board, task.id)
        self.assertEqual(runtime["attempt_status"], "running")
        self.assertEqual(runtime["redispatch_count"], 0)
        self.assertIn("stuck_alive", service.store.pending_outbox()[0]["payload"])

    def test_stale_heartbeat_with_fresh_progress_only_probes(self) -> None:
        now = int(time.time())
        service, task, managed = self._watchdog_threshold_fixture(
            "fresh_progress",
            started_at=now - 400,
        )
        service.store.record_card_runtime_event(
            board=managed.board,
            card_id=managed.card_id,
            kind="progress",
            created_at=now,
            worker_session_id="kanban-run:4",
            worker_pid=404,
            lease_seconds=service.config.worker_progress_lease_seconds,
            run_id=f"{managed.board}:4",
        )
        calls: list[bool] = []
        service.worker_recovery = type(
            "ProbeOnlyRecovery",
            (),
            {
                "observe": staticmethod(
                    lambda identity, terminate_running: (
                        calls.append(terminate_running)
                        or SupervisorObservation(
                            "running",
                            now,
                            identity.worker_pid,
                            process_count=1,
                        )
                    )
                )
            },
        )()

        service._enqueue_stale_worker_notices()

        self.assertEqual(calls, [False])
        runtime = service.store.card_runtime(managed.board, task.id)
        self.assertEqual(runtime["attempt_status"], "running")
        self.assertEqual(runtime["redispatch_count"], 0)
        self.assertIn(
            "liveness_unconfirmed",
            service.store.pending_outbox()[0]["payload"],
        )

    def test_watchdog_redispatches_only_after_confirmed_exit_and_identity_checks(
        self,
    ) -> None:
        task = task_record(
            task_id="t_stale",
            body="worker body",
            status="running",
            assignee="coder",
            idempotency_key="stale-work",
            tenant=self.run.run_key,
            current_run_id=4,
            worker_pid=999_999,
        )
        managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=task.id,
            run_key=self.run.run_key,
            stage=Stage.IMPLEMENT.value,
            iteration=1,
            idempotency_key="stale-work",
            parent_card_id="t_parent",
            purpose="work",
            created_at=1,
        )
        service = object.__new__(ControllerService)
        service.config = config(self.root)
        service.store = ControllerStore(self.root / "watchdog-controller.db")
        service.store.save_run(self.run)
        service.store.ensure_run_control(self.run.run_key)
        service.store.add_managed_card(
            board=managed.board,
            card_id=managed.card_id,
            run_key=managed.run_key,
            stage=managed.stage,
            iteration=managed.iteration,
            idempotency_key=managed.idempotency_key,
            parent_card_id=managed.parent_card_id,
            purpose=managed.purpose,
            created_at=managed.created_at,
        )
        service.store.register_card_attempt(
            board=managed.board,
            card_id=managed.card_id,
            profile="coder",
            dispatch_key=managed.idempotency_key,
            worktree=self.run.workspace.worktree,
            branch=self.run.workspace.branch,
        )
        service.store.record_card_runtime_event(
            board=managed.board,
            card_id=managed.card_id,
            kind="worker_started",
            created_at=1,
            worker_session_id="kanban-run:4",
            worker_pid=999_999,
            lease_seconds=300,
            run_id=f"{managed.board}:4",
        )
        service.reader = type(
            "WatchdogReader",
            (),
            {"task": staticmethod(lambda board, task_id: task)},
        )()
        service._history = lambda _: ([HistoryItem(managed, task)], self.run)
        live_mr: list[dict | None] = [
            {"iid": 2, "sha": "e" * 40},
        ]
        service.gitlab = type(
            "WatchdogGitLab",
            (),
            {
                "local_workspace_state": staticmethod(
                    lambda run: {
                        "ok": True,
                        "branch": run.workspace.branch,
                        "head_sha": "d" * 40,
                    }
                ),
                "delivery_mr": staticmethod(
                    lambda run, mr_iid=None: live_mr[0]
                ),
            },
        )()
        attach_test_store(service, self.root, self.run, delivery=True)
        redispatched: list[str] = []
        service.kanban = type(
            "WatchdogKanban",
            (),
            {
                "redispatch_stale_worker": staticmethod(
                    lambda board, task_id, reason, **kwargs: redispatched.append(
                        task_id
                    )
                )
            },
        )()
        service.worker_recovery = type(
            "ConfirmedExitRecovery",
            (),
            {
                "observe": staticmethod(
                    lambda identity, terminate_running: SupervisorObservation(
                        "terminated", 10, identity.worker_pid
                    )
                )
            },
        )()

        service._enqueue_stale_worker_notices()

        self.assertEqual(redispatched, [])
        self.assertIn(
            "mr_head_mismatch",
            service.store.pending_outbox()[0]["payload"],
        )
        live_mr[0] = {"iid": 2, "sha": "d" * 40}
        service._enqueue_stale_worker_notices()

        self.assertEqual(redispatched, [task.id])
        runtime = service.store.card_runtime(managed.board, managed.card_id)
        self.assertEqual(runtime["attempt_status"], "redispatch_requested")
        self.assertEqual(runtime["redispatch_count"], 1)
        self.assertEqual(len(service.store.pending_outbox()), 2)

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
        service.store.save_run(self.run)
        service.config = config(self.root).model_copy(
            update={"notification_level": NotificationLevel.VERBOSE}
        )
        service._history = lambda _: ([], self.run)
        second_task = task_record(
            task_id="t_review",
            body="body",
            status="running",
            assignee="code-reviewer",
        )
        service.reader = type(
            "LifecycleReader",
            (),
            {
                "task": staticmethod(
                    lambda board, card_id: (
                        task if card_id == task.id else second_task
                    )
                )
            },
        )()
        event = EventRecord(
            id=7,
            task_id=task.id,
            run_id=8,
            kind="claimed",
            payload={},
            created_at=9,
        )
        service.store.add_managed_card(
            board=managed.board,
            card_id=managed.card_id,
            run_key=managed.run_key,
            stage=managed.stage,
            iteration=managed.iteration,
            idempotency_key=managed.idempotency_key,
            parent_card_id=managed.parent_card_id,
            purpose=managed.purpose,
            created_at=managed.created_at,
        )

        service._record_agent_lifecycle_event(managed, event)
        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertIn("Agent 已开始工作", pending[0]["payload"])
        second_managed = ManagedCard(
            board=self.run.workspace.board,
            card_id=second_task.id,
            run_key=self.run.run_key,
            stage=Stage.CODE_REVIEW.value,
            iteration=1,
            idempotency_key="review",
            parent_card_id=task.id,
            purpose="work",
            created_at=2,
        )
        service.store.add_managed_card(
            board=second_managed.board,
            card_id=second_managed.card_id,
            run_key=second_managed.run_key,
            stage=second_managed.stage,
            iteration=second_managed.iteration,
            idempotency_key=second_managed.idempotency_key,
            parent_card_id=second_managed.parent_card_id,
            purpose=second_managed.purpose,
            created_at=second_managed.created_at,
        )
        service._record_agent_lifecycle_event(
            second_managed,
            EventRecord(
                id=8,
                task_id=second_task.id,
                run_id=9,
                kind="claimed",
                payload={},
                created_at=10,
            ),
        )
        self.assertEqual(len(service.store.pending_outbox()), 2)

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
        self.assertEqual(len(service.store.pending_outbox()), 2)

    def test_minimal_level_only_keeps_explicit_human_action_progress(
        self,
    ) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "minimal-controller.db")
        service.config = config(self.root).model_copy(
            update={"notification_level": NotificationLevel.MINIMAL}
        )

        service._enqueue_progress(self.run, "phase", "ordinary progress")
        service._enqueue_progress(
            self.run,
            "approval",
            "human approval required",
            allow_minimal=True,
        )

        pending = service.store.pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertIn("human approval required", pending[0]["payload"])


if __name__ == "__main__":
    unittest.main()
