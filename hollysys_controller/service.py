from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from .config import ControllerConfig
from .gitlab import CheckedHeadConflict, GitLabClient
from .kanban import (
    EventRecord,
    KanbanCLI,
    KanbanReader,
    TaskRecord,
    parse_card_body,
    parse_run_body,
)
from .models import (
    CardRecord,
    CompletionMetadata,
    FeishuOrigin,
    Outcome,
    ResolveRequest,
    RunRecord,
    Stage,
    StartRequest,
)
from .notifier import LarkNotifier
from .store import ControllerStore, ManagedCard
from .workflow import protocol_retry_allowed, route_completion

ACTIVE_STATUSES = {"triage", "todo", "ready", "running", "blocked"}
TERMINAL_EVENT_KINDS = {
    "completed",
    "blocked",
    "crashed",
    "timed_out",
    "gave_up",
    "spawn_auto_blocked",
    "status",
}


@dataclass
class HistoryItem:
    managed: ManagedCard
    task: TaskRecord


class ControllerService:
    def __init__(
        self,
        config: ControllerConfig,
        *,
        store: ControllerStore | None = None,
        reader: KanbanReader | None = None,
        kanban: KanbanCLI | None = None,
        gitlab: GitLabClient | None = None,
        notifier: LarkNotifier | None = None,
    ):
        self.config = config
        config.state_dir.mkdir(parents=True, exist_ok=True)
        self.store = store or ControllerStore(config.state_dir / "controller.db")
        self.reader = reader or KanbanReader(config.hermes_home)
        self.kanban = kanban or KanbanCLI(config, self.reader)
        self.gitlab = gitlab or GitLabClient(config)
        self.notifier = notifier or LarkNotifier(config)
        self._lock = threading.RLock()
        self.last_reconcile_at: int | None = None
        self.last_reconcile_error: str | None = None

    def start(self, raw: dict) -> dict:
        with self._lock:
            return self._start(raw)

    def _start(self, raw: dict) -> dict:
        request = StartRequest.model_validate(raw)
        key = f"start:{request.message_id}"
        previous = self.store.begin_request(
            key, "start", request.model_dump(mode="json")
        )
        if previous is not None:
            return previous
        try:
            origin = FeishuOrigin(
                message_id=request.message_id,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
                chat_type=request.chat_type,
                initiator_open_id=request.initiator,
            )
            facts = self.gitlab.validate_start(
                prd_blob_url=str(request.prd_blob_url),
                prd_mr_url=str(request.prd_mr_url),
                origin=origin,
            )
            run = facts.run
            existing = self.store.cards_for_run(run.run_key)
            if existing:
                self.reconcile_run(run.run_key)
                response = self.status(run.run_key)
                self.store.finish_request(key, response)
                return response

            self._operation(
                f"{run.run_key}:workspace",
                "workspace",
                {"run_key": run.run_key, "base_sha": facts.base_sha},
                lambda: (
                    self.gitlab.ensure_workspace(run, facts.base_sha)
                    or {"worktree": run.workspace.worktree}
                ),
            )
            self._operation(
                f"{run.run_key}:board",
                "board",
                {
                    "board": run.workspace.board,
                    "name": run.project.project_display_name,
                    "worktree": run.workspace.worktree,
                },
                lambda: (
                    self.kanban.ensure_board(
                        run.workspace.board,
                        run.project.project_display_name,
                        run.workspace.worktree,
                    )
                    or {"board": run.workspace.board}
                ),
            )
            root = self.kanban.create_root(run)
            self._verify_root(root, run)
            self.store.add_managed_card(
                board=run.workspace.board,
                card_id=root.id,
                run_key=run.run_key,
                stage="run-init",
                iteration=0,
                idempotency_key=f"{run.run_key}:run-init",
                parent_card_id=None,
                purpose="root",
                created_at=root.created_at,
            )
            self._operation(
                f"{run.run_key}:complete-root",
                "complete-root",
                {"board": run.workspace.board, "card_id": root.id},
                lambda: self.kanban.complete_root(run, root.id) or {"card_id": root.id},
            )
            first = self._create_work(run, Stage.SPEC_WRITE, root.id)
            response = {
                "run_key": run.run_key,
                "project": run.project.project_path,
                "stage": Stage.SPEC_WRITE.value,
                "active_card": first.id,
                "board": run.workspace.board,
                "worktree": run.workspace.worktree,
            }
            self.store.finish_request(key, response)
            return response
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise

    def status(self, run_key: str) -> dict:
        with self._lock:
            history, run = self._history(run_key)
            active = [item for item in history if item.task.status in ACTIVE_STATUSES]
            attempts = self._attempts_by_stage(history)
            protocol_failures = self._protocol_failures_by_stage(history)
            mr = self.gitlab.delivery_mr(run)
            merged = bool(mr and mr.get("state") == "merged")
            gates: dict[str, dict] = {}
            gate_authors: dict[Stage, str | None] = {}
            for stage in (
                Stage.SPEC_REVIEW,
                Stage.PLAN_REVIEW,
                Stage.TASKS_REVIEW,
                Stage.TEST,
                Stage.CODE_REVIEW,
            ):
                meta = self._latest_valid_pass(history, stage)
                valid = False
                reason = "no applicable passing completion"
                if meta is not None:
                    try:
                        gate_authors[stage] = self.gitlab.validate_gate(run, meta)
                        if (
                            stage
                            in {
                                Stage.SPEC_REVIEW,
                                Stage.PLAN_REVIEW,
                                Stage.TASKS_REVIEW,
                            }
                            and mr
                            and mr.get("sha")
                        ):
                            self.gitlab.validate_artifact_gate_at_ref(
                                run, meta, str(mr["sha"])
                            )
                        valid = True
                        reason = None
                    except Exception as exc:  # noqa: BLE001 - status reports failures
                        reason = str(exc)
                gates[stage.value] = {
                    "valid": valid,
                    "reason": reason,
                    "author": gate_authors.get(stage),
                    "head_sha": meta.head_sha if meta else None,
                    "review_commit_sha": meta.review_commit_sha if meta else None,
                    "artifact_digest": meta.artifact_digest if meta else None,
                }
            test_author = gate_authors.get(Stage.TEST)
            review_author = gate_authors.get(Stage.CODE_REVIEW)
            if (
                gates[Stage.TEST.value]["valid"]
                and gates[Stage.CODE_REVIEW.value]["valid"]
                and test_author == review_author
            ):
                reason = "test and code-review were published by the same GitLab user"
                for stage in (Stage.TEST, Stage.CODE_REVIEW):
                    gates[stage.value]["valid"] = False
                    gates[stage.value]["reason"] = reason
            current = active[-1] if active else None
            latest_work = next(
                (item for item in reversed(history) if item.managed.purpose == "work"),
                None,
            )
            blocked_comment = None
            if current and current.task.status in {"blocked", "triage"}:
                blocked_comment = next(
                    (
                        comment["body"]
                        for comment in reversed(current.task.comments)
                        if "[human-block:v1]" in comment["body"]
                    ),
                    current.task.latest_summary,
                )
                if blocked_comment is None and current.managed.purpose == "exception":
                    blocked_comment = current.task.body
            merge_blocker = None
            if not current and not merged:
                test = self._latest_valid_pass(history, Stage.TEST)
                review = self._latest_valid_pass(history, Stage.CODE_REVIEW)
                if test is None or review is None or test.mr_iid is None:
                    merge_blocker = (
                        "current test/code-review gates are incomplete or stale"
                    )
                else:
                    try:
                        self.gitlab.validate_merge(
                            run,
                            mr_iid=test.mr_iid,
                            test=test,
                            code_review=review,
                        )
                    except Exception as exc:  # noqa: BLE001 - live status fact
                        merge_blocker = self._error_text(exc)
            return {
                "run_key": run_key,
                "phase": (
                    "merged"
                    if merged
                    else current.managed.stage
                    if current
                    else "checked-head-merge"
                    if latest_work
                    and latest_work.managed.stage == Stage.CODE_REVIEW.value
                    and latest_work.task.status == "done"
                    else "reconciling"
                ),
                "active_card": (
                    {
                        "id": current.task.id,
                        "stage": current.managed.stage,
                        "iteration": current.managed.iteration,
                        "status": current.task.status,
                        "purpose": current.managed.purpose,
                    }
                    if current
                    else None
                ),
                "attempts": {stage.value: count for stage, count in attempts.items()},
                "protocol_failures": {
                    stage.value: count for stage, count in protocol_failures.items()
                },
                "mr": (
                    {
                        "iid": mr.get("iid"),
                        "url": mr.get("web_url"),
                        "head_sha": mr.get("sha"),
                        "state": mr.get("state"),
                        "draft": mr.get("draft") or mr.get("work_in_progress"),
                    }
                    if mr
                    else None
                ),
                "gates": gates,
                "blocked": blocked_comment,
                "merge_blocker": merge_blocker,
                "board": run.workspace.board,
                "worktree": run.workspace.worktree,
            }

    def resolve(self, raw: dict) -> dict:
        request = ResolveRequest.model_validate(raw)
        key = f"resolve:{request.block_id}:{request.message_id}"
        previous = self.store.begin_request(
            key, "resolve", request.model_dump(mode="json")
        )
        if previous is not None:
            return previous
        try:
            with self._lock:
                history, run = self._history(request.run_key)
                managed = next(
                    (
                        item.managed
                        for item in history
                        if item.task.id == request.card_id
                    ),
                    None,
                )
                task = next(
                    (item.task for item in history if item.task.id == request.card_id),
                    None,
                )
                if managed is None or task is None or managed.purpose != "work":
                    raise ValueError("card is not a managed Hollysys work card")
                origin = run.origin
                if (
                    request.sender != origin.initiator_open_id
                    or request.chat_id != origin.chat_id
                    or (request.thread_id or None) != (origin.thread_id or None)
                ):
                    raise PermissionError(
                        "resolution must come from the original initiator and channel"
                    )
                block_comment = next(
                    (
                        str(comment["body"])
                        for comment in reversed(task.comments)
                        if "[human-block:v1]" in str(comment["body"])
                        and f"block_id: {request.block_id}" in str(comment["body"])
                    ),
                    None,
                )
                if block_comment is None:
                    raise ValueError("matching [human-block:v1] comment was not found")
                stage = Stage(managed.stage)
                retry = self._resolved_retry(
                    history,
                    task.id,
                    stage,
                    request.answer,
                )
                if retry is None:
                    if task.status != "blocked":
                        raise ValueError("card is not currently blocked")
                    retry = self._create_work(
                        run,
                        stage,
                        task.id,
                        resume_answer=request.answer,
                        resumed_from=task.id,
                    )
                else:
                    if retry.status in ACTIVE_STATUSES:
                        self._ensure_work_published(run, retry)
                resolution = (
                    "[human-resolution:v1]\n"
                    f"block_id: {request.block_id}\n"
                    f"message_id: {request.message_id}\n"
                    f"resolved_by: {request.sender}\n"
                    f"answer: {request.answer}\n"
                    f"new_card_id: {retry.id}"
                )
                resolution_exists = any(
                    f"message_id: {request.message_id}" in str(comment["body"])
                    for comment in task.comments
                )
                if not resolution_exists:
                    self.kanban.comment(
                        run.workspace.board, task.id, resolution, "hollysys-controller"
                    )
                    resolution_exists = True
                if task.status == "blocked":
                    self.kanban.unsubscribe(run.workspace.board, task.id, run.origin)
                    cancelled = self._controller_completion(
                        run,
                        managed,
                        task.id,
                        outcome=Outcome.CANCELLED,
                        issues=[f"human block resolved; superseded by {retry.id}"],
                    )
                    self.kanban.complete(
                        run.workspace.board,
                        task.id,
                        (
                            f"Human block {request.block_id} was resolved; "
                            f"retry is {retry.id}."
                        ),
                        cancelled,
                    )
                elif task.status != "done" or not resolution_exists:
                    raise ValueError("card is no longer a resolvable human block")
                event_key = f"{run.run_key}:resumed:{request.block_id}"
                self.store.enqueue(
                    event_key,
                    run.run_key,
                    "resumed",
                    {
                        "origin": run.origin.model_dump(mode="json"),
                        "text": self._mention(run.origin)
                        + f"已记录并恢复自动交付。\nrun={run.run_key} "
                        f"resolved={task.id} stage={stage.value} next={retry.id}",
                    },
                )
                response = {
                    "run_key": run.run_key,
                    "resolved_card": task.id,
                    "new_card": retry.id,
                    "stage": stage.value,
                }
                self.store.finish_request(key, response)
                return response
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise

    def poll_once(self) -> None:
        with self._lock:
            boards = self.reader.discover_boards()
            for card in [
                card
                for run_key in self.store.run_keys()
                for card in self.store.cards_for_run(run_key)
            ]:
                path = self.reader.board_db(card.board)
                if path.is_file():
                    boards.setdefault(card.board, path)
            for board in sorted(boards):
                cursor = self.store.cursor(board)
                for event in self.reader.events_after(board, cursor):
                    managed = self.store.managed_card(board, event.task_id)
                    if managed and event.kind in TERMINAL_EVENT_KINDS:
                        try:
                            if event.kind in {"gave_up", "spawn_auto_blocked"}:
                                _, run = self._history(managed.run_key)
                                self._enqueue_failure_limit(run, event)
                            self.reconcile_run(managed.run_key)
                        except Exception as exc:
                            self._enqueue_controller_failure(managed.run_key, exc)
                            raise
                    self.store.set_cursor(board, event.id)
            self.flush_outbox()

    def reconcile_all(self) -> None:
        with self._lock:
            try:
                for request in self.store.running_requests():
                    if request["kind"] == "start":
                        self.start(request["payload"])
                    elif request["kind"] == "resolve":
                        self.resolve(request["payload"])
                run_errors: list[str] = []
                for run_key in self.store.run_keys():
                    try:
                        self.reconcile_run(run_key)
                    except Exception as exc:  # noqa: BLE001 - isolate each run
                        self._enqueue_controller_failure(run_key, exc)
                        run_errors.append(f"{run_key}: {exc}")
                if run_errors:
                    raise RuntimeError("; ".join(run_errors))
                self.last_reconcile_at = int(time.time())
                self.last_reconcile_error = None
            except Exception as exc:
                self.last_reconcile_error = str(exc)
                raise

    def reconcile_run(self, run_key: str) -> None:
        history, run = self._history(run_key)
        mr = self.gitlab.delivery_mr(run)
        if mr and mr.get("state") == "merged":
            self._enqueue_success(run, mr)
            return
        active = [item for item in history if item.task.status in ACTIVE_STATUSES]
        active_root = next(
            (
                item
                for item in active
                if item.managed.purpose == "root" and item.task.status == "blocked"
            ),
            None,
        )
        if active_root:
            self.kanban.complete_root(run, active_root.task.id)
            self._create_work(run, Stage.SPEC_WRITE, active_root.task.id)
            return
        for item in active:
            if item.managed.purpose != "work":
                continue
            if not self.reader.subscription_exists(
                run.workspace.board, item.task.id, run.origin
            ):
                self.kanban.subscribe(run.workspace.board, item.task.id, run.origin)
            if item.task.status == "blocked":
                failure_fuse = any(
                    kind in {"gave_up", "spawn_auto_blocked"}
                    for kind in item.task.event_kinds
                )
                human_block = any(
                    "[human-block:v1]" in str(comment["body"])
                    for comment in item.task.comments
                )
                if item.task.latest_outcome is None and not failure_fuse:
                    # This is an interrupted controller publish, not a worker
                    # block. Initial status events vary by Hermes version, so
                    # the absence of a task-run outcome is the stable signal.
                    self.kanban.release(run.workspace.board, item.task.id)
                    return
                if item.task.latest_outcome == "blocked" and not human_block:
                    self._enqueue_controller_failure(
                        run.run_key,
                        ValueError(
                            f"blocked card {item.task.id} has no "
                            "[human-block:v1] comment"
                        ),
                    )
        if len(active) > 1:
            # A blocked parent plus its todo retry is a valid, short resolve
            # transaction; all other multi-active shapes violate seriality.
            valid_resolution_window = (
                len(active) == 2
                and active[0].task.status == "blocked"
                and active[1].task.status in {"todo", "blocked"}
                and active[1].managed.parent_card_id == active[0].task.id
            )
            if not valid_resolution_window:
                self._exception(
                    run,
                    history[-1].task.id,
                    "more than one managed card is active",
                )
            return
        if active:
            return
        work = [item for item in history if item.managed.purpose == "work"]
        if not work:
            root = next(item for item in history if item.managed.purpose == "root")
            self._create_work(run, Stage.SPEC_WRITE, root.task.id)
            return
        latest = work[-1]
        if latest.task.status != "done":
            return
        try:
            metadata = CompletionMetadata.model_validate(latest.task.latest_metadata)
            self._validate_completion_context(run, latest, metadata)
        except (ValidationError, ValueError, TypeError) as exc:
            self._protocol_failure(run, history, latest, self._error_text(exc))
            return

        if metadata.outcome == Outcome.PASS:
            try:
                self.gitlab.validate_gate(run, metadata)
            except ValueError as exc:
                # A push invalidates test/code-review and deterministically
                # restarts at test; other gate mismatches are protocol retries.
                if metadata.stage in {Stage.TEST, Stage.CODE_REVIEW} and (
                    "current MR head" in str(exc) or "not bound" in str(exc)
                ):
                    self._create_work(run, Stage.TEST, latest.task.id)
                else:
                    self._protocol_failure(run, history, latest, str(exc))
                return
            if metadata.stage in {
                Stage.SPEC_REVIEW,
                Stage.PLAN_REVIEW,
                Stage.TASKS_REVIEW,
            }:
                live_mr = self.gitlab.delivery_mr(run, metadata.mr_iid)
                if live_mr is None or not live_mr.get("sha"):
                    self._protocol_failure(
                        run,
                        history,
                        latest,
                        "delivery MR has no current head",
                    )
                    return
                try:
                    self.gitlab.validate_artifact_gate_at_ref(
                        run, metadata, str(live_mr["sha"])
                    )
                except ValueError:
                    producer = {
                        Stage.SPEC_REVIEW: Stage.SPEC_WRITE,
                        Stage.PLAN_REVIEW: Stage.PLAN_WRITE,
                        Stage.TASKS_REVIEW: Stage.TASKS_WRITE,
                    }[metadata.stage]
                    self._restart_with_budget(run, history, producer, latest.task.id)
                    return

        attempts = self._attempts_by_stage(history)
        route = route_completion(
            metadata, attempts_by_stage=attempts, config=self.config
        )
        if route.blocked_reason:
            self._exception(run, latest.task.id, route.blocked_reason)
            return
        if route.next_stage:
            self._create_work(run, route.next_stage, latest.task.id)
            return
        if route.merge:
            test = self._latest_valid_pass(history, Stage.TEST)
            review = self._latest_valid_pass(history, Stage.CODE_REVIEW)
            if test is None or review is None or test.mr_iid is None:
                self._create_work(run, Stage.TEST, latest.task.id)
                return
            live_mr = self.gitlab.delivery_mr(run, test.mr_iid)
            if live_mr is None or not live_mr.get("sha"):
                return
            current_head = str(live_mr["sha"])
            document_gates = (
                (Stage.SPEC_REVIEW, Stage.SPEC_WRITE),
                (Stage.PLAN_REVIEW, Stage.PLAN_WRITE),
                (Stage.TASKS_REVIEW, Stage.TASKS_WRITE),
            )
            for gate_stage, producer_stage in document_gates:
                gate = self._latest_valid_pass(history, gate_stage)
                if gate is None:
                    self._restart_with_budget(
                        run, history, producer_stage, latest.task.id
                    )
                    return
                try:
                    self.gitlab.validate_gate(run, gate)
                    self.gitlab.validate_artifact_gate_at_ref(run, gate, current_head)
                except ValueError:
                    self._restart_with_budget(
                        run, history, producer_stage, latest.task.id
                    )
                    return
            try:
                self.gitlab.validate_gate(run, test)
                self.gitlab.validate_gate(run, review)
            except ValueError:
                self._create_work(run, Stage.TEST, latest.task.id)
                return
            try:
                mr, checked_head = self.gitlab.validate_merge(
                    run,
                    mr_iid=test.mr_iid,
                    test=test,
                    code_review=review,
                )
            except ValueError as exc:
                if "current MR head" in str(exc):
                    self._create_work(run, Stage.TEST, latest.task.id)
                # Pipeline/discussion/MR readiness is an external fact. Leave
                # the run cardless and let the 30-second reconcile retry it.
                return
            try:
                merged = self._operation(
                    f"{run.run_key}:merge:{checked_head}",
                    "checked-head-merge",
                    {
                        "project_id": run.project.project_id,
                        "mr_iid": int(mr["iid"]),
                        "checked_head": checked_head,
                    },
                    lambda: self.gitlab.merge(run, int(mr["iid"]), checked_head),
                )
            except CheckedHeadConflict:
                self._create_work(run, Stage.TEST, latest.task.id)
                return
            self._enqueue_success(run, merged)

    def _restart_with_budget(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        stage: Stage,
        parent_card_id: str,
    ) -> None:
        attempts = self._attempts_by_stage(history).get(stage, 0)
        if attempts >= 1 + self.config.design_rework_limit:
            self._exception(
                run,
                parent_card_id,
                f"{stage.value} rework budget exhausted after gate invalidation",
            )
            return
        self._create_work(run, stage, parent_card_id)

    def flush_outbox(self) -> None:
        for item in self.store.pending_outbox():
            try:
                payload = json.loads(item["payload"])
                origin = FeishuOrigin.model_validate(payload["origin"])
                self.notifier.send(item["outbox_key"], origin, payload["text"])
                self.store.finish_outbox(item["outbox_key"])
            except Exception as exc:  # noqa: BLE001 - durable outbox boundary
                self.store.fail_outbox(item["outbox_key"], str(exc))

    def health(self) -> dict:
        data = self.store.health()
        boards = self.reader.discover_boards()
        board_health: dict[str, dict] = {}
        for board in sorted(boards):
            try:
                board_health[board] = {
                    "ok": True,
                    "max_event_id": self.reader.max_event_id(board),
                }
            except Exception as exc:  # noqa: BLE001 - health reports degradation
                board_health[board] = {"ok": False, "error": str(exc)}
        kanban_ok = all(item["ok"] for item in board_health.values()) and (
            bool(boards) or not self.store.run_keys()
        )
        data.update(
            {
                "ok": self.last_reconcile_error is None,
                "boards": sorted(boards),
                "board_health": board_health,
                "last_reconcile_at": self.last_reconcile_at,
                "last_reconcile_error": self.last_reconcile_error,
                "kanban_ok": kanban_ok,
            }
        )
        data["ok"] = data["ok"] and kanban_ok
        try:
            data["gitlab"] = self.gitlab.health()
            data["ok"] = data["ok"] and bool(data["gitlab"]["ok"])
        except Exception as exc:  # noqa: BLE001 - health must return degraded state
            data["gitlab"] = {"ok": False, "error": str(exc)}
            data["ok"] = False
        return data

    def _create_work(
        self,
        run: RunRecord,
        stage: Stage,
        parent_card_id: str,
        *,
        resume_answer: str | None = None,
        resumed_from: str | None = None,
    ) -> TaskRecord:
        history = self.store.cards_for_run(run.run_key)
        attempts = sum(
            1
            for card in history
            if card.purpose == "work" and card.stage == stage.value
        )
        iteration = attempts + 1
        key = f"{run.run_key}:{stage.value}:{iteration}:work"
        assignee = self.config.stage_assignees[stage]
        skills = self.config.stage_skills[stage]
        record = CardRecord(
            run=run,
            stage=stage,
            iteration=iteration,
            idempotency_key=key,
            parent_card_id=parent_card_id,
            assignee=assignee,
            skills=skills,
            resume_answer=resume_answer,
            resumed_from_card_id=resumed_from,
        )
        task = self.kanban.create_work(record)
        self._verify_work(task, record)
        self.store.add_managed_card(
            board=run.workspace.board,
            card_id=task.id,
            run_key=run.run_key,
            stage=stage.value,
            iteration=iteration,
            idempotency_key=key,
            parent_card_id=parent_card_id,
            purpose="work",
            created_at=task.created_at,
        )
        return self._ensure_work_published(run, task)

    def _ensure_work_published(self, run: RunRecord, task: TaskRecord) -> TaskRecord:
        record = parse_card_body(task.body)
        key = record.idempotency_key
        self._operation(
            f"{key}:subscribe",
            "subscribe",
            {"board": run.workspace.board, "card_id": task.id},
            lambda: (
                self.kanban.subscribe(run.workspace.board, task.id, run.origin)
                or {"card_id": task.id}
            ),
        )
        if not self.reader.subscription_exists(
            run.workspace.board, task.id, run.origin
        ):
            # The durable operation may predate an external subscription
            # deletion. Repair the observable fact before releasing the card.
            self.kanban.subscribe(run.workspace.board, task.id, run.origin)
        self._operation(
            f"{key}:release",
            "release",
            {"board": run.workspace.board, "card_id": task.id},
            lambda: (
                self.kanban.release(run.workspace.board, task.id)
                or {"card_id": task.id}
            ),
        )
        refreshed = self.reader.task(run.workspace.board, task.id)
        if refreshed is None:
            raise RuntimeError(f"released card {task.id} disappeared")
        return refreshed

    def _resolved_retry(
        self,
        history: list[HistoryItem],
        blocked_card_id: str,
        stage: Stage,
        answer: str,
    ) -> TaskRecord | None:
        for item in reversed(history):
            if (
                item.managed.purpose != "work"
                or item.managed.stage != stage.value
                or item.managed.parent_card_id != blocked_card_id
            ):
                continue
            try:
                record = parse_card_body(item.task.body)
            except (ValidationError, ValueError, json.JSONDecodeError):
                continue
            if (
                record.resumed_from_card_id == blocked_card_id
                and record.resume_answer == answer
            ):
                return item.task
        return None

    def _verify_work(self, task: TaskRecord, record: CardRecord) -> None:
        if (
            task.created_by != "hollysys-controller"
            or task.idempotency_key != record.idempotency_key
            or task.tenant != record.run.run_key
            or task.assignee != record.assignee
            or task.parents != [record.parent_card_id]
            or sorted(task.skills) != sorted(record.skills)
        ):
            raise ValueError(f"created card {task.id} does not match controller intent")
        parsed = parse_card_body(task.body)
        if parsed != record:
            raise ValueError(f"created card {task.id} body changed during creation")

    def _verify_root(self, task: TaskRecord, run: RunRecord) -> None:
        if (
            task.created_by != "hollysys-controller"
            or task.idempotency_key != f"{run.run_key}:run-init"
            or task.tenant != run.run_key
            or task.parents
            or parse_run_body(task.body) != run
        ):
            raise ValueError(f"created run root {task.id} does not match intent")

    def _history(self, run_key: str) -> tuple[list[HistoryItem], RunRecord]:
        cards = self.store.cards_for_run(run_key)
        if not cards:
            raise ValueError(f"unknown run {run_key}")
        items: list[HistoryItem] = []
        for card in cards:
            task = self.reader.task(card.board, card.card_id)
            if task is None:
                raise RuntimeError(f"managed card disappeared: {card.card_id}")
            items.append(HistoryItem(card, task))
        root_item = next(
            (item for item in items if item.managed.purpose == "root"), None
        )
        if root_item is None:
            raise RuntimeError(f"run {run_key} has no managed root")
        run = parse_run_body(root_item.task.body)
        self._verify_root(root_item.task, run)
        if root_item.managed.run_key != run.run_key:
            raise ValueError(f"managed run root {root_item.task.id} was modified")
        for item in items:
            if item.managed.purpose != "work":
                continue
            record = parse_card_body(item.task.body)
            if (
                record.run != run
                or record.stage.value != item.managed.stage
                or record.iteration != item.managed.iteration
                or record.idempotency_key != item.managed.idempotency_key
                or record.parent_card_id != item.managed.parent_card_id
            ):
                raise ValueError(f"managed card {item.task.id} body was modified")
            self._verify_work(item.task, record)
        return items, run

    def _attempts_by_stage(self, history: list[HistoryItem]) -> dict[Stage, int]:
        result: dict[Stage, int] = {}
        for item in history:
            if item.managed.purpose != "work":
                continue
            stage = Stage(item.managed.stage)
            if any(
                "[controller-protocol-error:v1]" in str(comment["body"])
                for comment in item.task.comments
            ):
                continue
            result[stage] = result.get(stage, 0) + 1
        return result

    def _protocol_failures_by_stage(
        self, history: list[HistoryItem]
    ) -> dict[Stage, int]:
        result: dict[Stage, int] = {}
        for item in history:
            if item.managed.purpose != "work":
                continue
            if any(
                "[controller-protocol-error:v1]" in str(comment["body"])
                for comment in item.task.comments
            ):
                stage = Stage(item.managed.stage)
                result[stage] = result.get(stage, 0) + 1
        return result

    def _validate_completion_context(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        expected = {
            "run_key": run.run_key,
            "stage": item.managed.stage,
            "iteration": item.managed.iteration,
            "project_id": run.project.project_id,
            "project_path": run.project.project_path,
            "checkout": run.workspace.checkout,
            "worktree": run.workspace.worktree,
            "branch": run.workspace.branch,
            "target_branch": run.workspace.target_branch,
            "prd_path": run.source.prd_path,
            "prd_commit_sha": run.source.prd_commit_sha,
            "prd_mr_url": str(run.source.prd_mr_url),
            "kanban_card_id": item.task.id,
        }
        actual = metadata.model_dump(mode="json")
        mismatches = [
            key for key, value in expected.items() if actual.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "completion context mismatch: " + ", ".join(sorted(mismatches))
            )

    def _protocol_failure(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        latest: HistoryItem,
        reason: str,
    ) -> None:
        marker = "[controller-protocol-error:v1]"
        already_marked = any(
            marker in str(comment["body"]) for comment in latest.task.comments
        )
        if not already_marked:
            self.kanban.comment(
                run.workspace.board,
                latest.task.id,
                f"{marker}\nreason: {reason[:1200]}",
                "hollysys-controller",
            )
        failures = self._protocol_failures_by_stage(history)
        invalid_count = failures.get(Stage(latest.managed.stage), 0)
        if not already_marked:
            invalid_count += 1
        if protocol_retry_allowed(invalid_count, self.config):
            self._create_work(run, Stage(latest.managed.stage), latest.task.id)
        else:
            self._exception(
                run,
                latest.task.id,
                f"metadata/gate protocol retry budget exhausted: {reason}",
            )

    def _exception(
        self, run: RunRecord, parent_card_id: str, reason: str
    ) -> TaskRecord:
        suffix = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
        key = f"{run.run_key}:exception:{suffix}:work"
        task = self.kanban.create_exception(run, parent_card_id, reason, key)
        if (
            task.created_by != "hollysys-controller"
            or task.idempotency_key != key
            or task.tenant != run.run_key
            or task.assignee != "dispatcher"
            or task.parents != [parent_card_id]
            or "hollysys-dispatch-kanban" not in task.skills
        ):
            raise ValueError(f"created exception card {task.id} does not match intent")
        self.store.add_managed_card(
            board=run.workspace.board,
            card_id=task.id,
            run_key=run.run_key,
            stage="exception",
            iteration=1,
            idempotency_key=key,
            parent_card_id=parent_card_id,
            purpose="exception",
            created_at=task.created_at,
        )
        self.kanban.subscribe(run.workspace.board, task.id, run.origin)
        outbox_key = f"{run.run_key}:exception:{suffix}"
        self.store.enqueue(
            outbox_key,
            run.run_key,
            "exception",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + f"自动交付需要异常处理。\nrun={run.run_key} "
                f"card={task.id}\nreason={reason[:500]}",
            },
        )
        return task

    def _controller_completion(
        self,
        run: RunRecord,
        managed: ManagedCard,
        card_id: str,
        *,
        outcome: Outcome,
        issues: list[str],
    ) -> dict:
        return CompletionMetadata(
            protocol_version="hollysys-controller/v1",
            run_key=run.run_key,
            stage=Stage(managed.stage),
            iteration=managed.iteration,
            outcome=outcome,
            project_id=run.project.project_id,
            project_path=run.project.project_path,
            checkout=run.workspace.checkout,
            worktree=run.workspace.worktree,
            branch=run.workspace.branch,
            target_branch=run.workspace.target_branch,
            prd_path=run.source.prd_path,
            prd_commit_sha=run.source.prd_commit_sha,
            prd_mr_url=run.source.prd_mr_url,
            kanban_card_id=card_id,
            issues=issues,
        ).model_dump(mode="json")

    def _latest_valid_pass(
        self, history: list[HistoryItem], stage: Stage
    ) -> CompletionMetadata | None:
        # A gate only remains relevant when it is after the latest producer
        # attempt that can invalidate it.
        producer = {
            Stage.SPEC_REVIEW: Stage.SPEC_WRITE,
            Stage.PLAN_REVIEW: Stage.PLAN_WRITE,
            Stage.TASKS_REVIEW: Stage.TASKS_WRITE,
            Stage.TEST: Stage.IMPLEMENT,
            Stage.CODE_REVIEW: Stage.IMPLEMENT,
        }[stage]
        producer_index = max(
            (
                index
                for index, item in enumerate(history)
                if item.managed.stage == producer.value
                and item.managed.purpose == "work"
            ),
            default=-1,
        )
        for index in range(len(history) - 1, producer_index, -1):
            item = history[index]
            if item.managed.stage != stage.value or item.task.status != "done":
                continue
            try:
                metadata = CompletionMetadata.model_validate(item.task.latest_metadata)
                self._validate_completion_context(
                    parse_card_body(item.task.body).run,
                    item,
                    metadata,
                )
            except (ValidationError, ValueError, TypeError):
                continue
            if metadata.outcome == Outcome.PASS:
                return metadata
        return None

    def _operation(
        self,
        key: str,
        kind: str,
        payload: dict,
        action: Callable[[], dict],
    ) -> dict:
        previous = self.store.operation_result(key, kind, payload)
        if previous is not None:
            return previous
        try:
            result = action()
            self.store.finish_operation(key, result)
            return result
        except Exception as exc:
            self.store.fail_operation(key, str(exc))
            raise

    def _enqueue_success(self, run: RunRecord, mr: dict) -> None:
        merge_sha = str(mr.get("merge_commit_sha") or "")
        key = f"{run.run_key}:merged:{merge_sha or mr.get('iid')}"
        self.store.enqueue(
            key,
            run.run_key,
            "merged",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin) + "PRD 自动交付完成。\n"
                f"run={run.run_key} project={run.project.project_display_name} "
                f"({run.project.project_path})\n"
                f"mr={mr.get('web_url')} merge={merge_sha}",
            },
        )

    def _enqueue_failure_limit(self, run: RunRecord, event: EventRecord) -> None:
        key = f"{run.run_key}:failure-limit:{event.task_id}"
        details = json.dumps(event.payload or {}, ensure_ascii=False)[:500]
        self.store.enqueue(
            key,
            run.run_key,
            "failure-limit",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "Hermes 工作卡已触发失败熔断，需要人工检查。\n"
                f"run={run.run_key} card={event.task_id} "
                f"event={event.kind}\ndetails={details}",
            },
        )

    def _enqueue_controller_failure(self, run_key: str, error: Exception) -> None:
        try:
            root = next(
                card
                for card in self.store.cards_for_run(run_key)
                if card.purpose == "root"
            )
            task = self.reader.task(root.board, root.card_id)
            if task is None:
                return
            run = parse_run_body(task.body)
        except (StopIteration, ValidationError, ValueError, json.JSONDecodeError):
            return
        reason = f"{type(error).__name__}: {self._error_text(error)}"
        suffix = hashlib.sha256(reason.encode()).hexdigest()[:12]
        self.store.enqueue(
            f"{run_key}:controller-failure:{suffix}",
            run_key,
            "controller-failure",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "Hollysys Controller 对账失败，需要管理员检查。\n"
                f"run={run_key}\nerror={reason[:700]}",
            },
        )

    @staticmethod
    def _mention(origin: FeishuOrigin) -> str:
        if origin.chat_type == "group" or origin.thread_id:
            return f'<at user_id="{origin.initiator_open_id}"></at> '
        return ""

    @staticmethod
    def _error_text(error: Exception) -> str:
        if isinstance(error, ValidationError):
            return json.dumps(
                error.errors(include_input=False, include_url=False),
                ensure_ascii=False,
            )
        return str(error)
