from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hollysys_controller.kanban import (
    EventRecord,
    TaskRecord,
    render_card_body,
    render_run_body,
)
from hollysys_controller.models import CardRecord, Stage
from hollysys_controller.service import ControllerService, HistoryItem
from hollysys_controller.store import ControllerStore, ManagedCard
from tests.helpers import run_record


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
        latest_metadata=None,
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
            "use the approved interface",
        )
        self.assertEqual(found, task)

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
        service.reader = type(
            "SubscribedReader",
            (),
            {"subscription_exists": staticmethod(lambda board, task_id, origin: True)},
        )()
        released: list[str] = []
        service.kanban = type(
            "ReleaseRecorder",
            (),
            {"release": staticmethod(lambda board, task_id: released.append(task_id))},
        )()

        service.reconcile_run(self.run.run_key)

        self.assertEqual(released, ["t_held"])

    def test_failure_limit_notification_is_idempotent(self) -> None:
        service = object.__new__(ControllerService)
        service.store = ControllerStore(self.root / "controller.db")
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
            comments=[{"body": "[controller-protocol-error:v1]\nreason: bad metadata"}],
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


if __name__ == "__main__":
    unittest.main()
