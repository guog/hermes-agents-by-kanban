from __future__ import annotations

import hashlib
import json
import logging
import secrets
import shutil
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
    AbortConfirmRequest,
    AbortRequest,
    ArtifactBaseline,
    BaselineDisposition,
    CardRecord,
    CompletionMetadata,
    CompletionValidationRequest,
    FeishuOrigin,
    NotificationLevel,
    Outcome,
    Phase,
    RepairContext,
    RepairKind,
    ResolveRequest,
    RunRecord,
    Stage,
    StartRequest,
    TestDisposition,
    WorkMode,
    validate_persisted_completion_metadata,
)
from .notifier import LarkNotifier
from .store import ControllerStore, ManagedCard
from .workflow import (
    DOCUMENT_REVIEW_FOR_PRODUCER,
    PHASE_FOR_STAGE,
    PRODUCER_FOR_PHASE,
    protocol_retry_allowed,
    route_completion,
)

ACTIVE_STATUSES = {"triage", "todo", "ready", "running", "blocked"}
LOG = logging.getLogger(__name__)
TERMINAL_EVENT_KINDS = {
    "completed",
    "blocked",
    "crashed",
    "timed_out",
    "gave_up",
    "spawn_auto_blocked",
    "status",
}
ALLOWED_HUMAN_BLOCK_KINDS = {
    "permission",
    "credential",
    "environment",
    "unsafe_retry",
    "destructive_approval",
}
RESOLVABLE_HUMAN_BLOCK_STATUSES = {
    "blocked",
    "triage",
    # These two states can be left by a process interruption after Controller
    # starts the audited triage -> todo -> ready transition.
    "todo",
    "ready",
}
REQUIRED_HUMAN_BLOCK_FIELDS = {
    "block_id",
    "kind",
    "summary",
    "evidence",
    "required_action",
    "resume_check",
}
GATED_HUMAN_BLOCK_FIELDS = {
    "gate_phase",
    "requirement_ids",
    "contract_refs",
}
PARSED_HUMAN_BLOCK_FIELDS = (
    REQUIRED_HUMAN_BLOCK_FIELDS | GATED_HUMAN_BLOCK_FIELDS
)


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
        self._audit_lock = threading.Lock()
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
            self.store.ensure_run_control(run.run_key)
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
            self._enqueue_progress(
                run,
                "run-accepted",
                self._mention(run.origin)
                + "已受理 PRD 自动交付并进入 SPEC。\n"
                f"run={run.run_key} agent={first.assignee} card={first.id}",
            )
            self._enqueue_phase_started(run, Phase.SPEC, first)
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
        # Full GitLab audit may be slow. Serialize audits separately so a
        # Dispatcher status request cannot block Kanban transitions, aborts,
        # local status-summary, or the outbox.
        with getattr(self, "_audit_lock", threading.Lock()):
            history, run = self._history(run_key)
            active = [item for item in history if item.task.status in ACTIVE_STATUSES]
            attempts = self._attempts_by_stage(history)
            document_review_attempts = self._review_attempts_by_stage(history)
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
                meta = self._latest_valid_completion(
                    history, stage, {Outcome.PASS, Outcome.FAIL}
                )
                valid = False
                evidence_valid = False
                reason = "no applicable completion"
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
                        evidence_valid = True
                        valid = meta.outcome == Outcome.PASS
                        reason = (
                            None
                            if valid
                            else "gate evidence is valid but outcome is fail"
                        )
                    except Exception as exc:  # noqa: BLE001 - status reports failures
                        reason = str(exc)
                gates[stage.value] = {
                    "valid": valid,
                    "evidence_valid": evidence_valid,
                    "outcome": meta.outcome.value if meta else None,
                    "reason": reason,
                    "author": gate_authors.get(stage),
                    "head_sha": meta.head_sha if meta else None,
                    "artifact_commit_sha": (
                        meta.artifact_commit_sha if meta else None
                    ),
                    "artifact_digest": meta.artifact_digest if meta else None,
                    "test_disposition": (
                        meta.test_disposition.value
                        if meta and meta.test_disposition
                        else None
                    ),
                    "skip_reason": meta.skip_reason if meta else None,
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
            current_record = (
                parse_card_body(current.task.body)
                if current and current.managed.purpose == "work"
                else None
            )
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
            frozen_artifacts: list[dict] = []
            key_decisions: list[dict] = []
            residual_risks: list[dict] = []
            current_head = str(mr.get("sha") or "") if mr else ""
            for baseline in self._frozen_baselines(history, run):
                valid: bool | None = None
                reason: str | None = None
                if current_head:
                    try:
                        self.gitlab.validate_baseline_at_ref(
                            run, baseline, current_head
                        )
                        valid = True
                    except Exception as exc:  # noqa: BLE001 - live status fact
                        valid = False
                        reason = self._error_text(exc)
                frozen_artifacts.append(
                    {
                        **baseline.model_dump(mode="json"),
                        "valid_at_current_head": valid,
                        "reason": reason,
                    }
                )
                if baseline.decision_urls:
                    key_decisions.append(
                        {
                            "phase": baseline.phase,
                            "disposition": baseline.disposition.value,
                            "summary": baseline.key_decisions,
                            "urls": [str(url) for url in baseline.decision_urls],
                        }
                    )
                if baseline.unresolved_findings or baseline.residual_risk:
                    residual_risks.append(
                        {
                            "phase": baseline.phase,
                            "unresolved_findings": baseline.unresolved_findings,
                            "residual_risks": baseline.residual_risk,
                        }
                    )
            review_stages = {
                Phase.SPEC: Stage.SPEC_REVIEW,
                Phase.PLAN: Stage.PLAN_REVIEW,
                Phase.TASKS: Stage.TASKS_REVIEW,
            }
            review_attempts = {
                phase.value: document_review_attempts.get(stage, 0)
                for phase, stage in review_stages.items()
            }
            review_remaining = {
                phase: max(0, self.config.document_review_limit - count)
                for phase, count in review_attempts.items()
            }
            code_modifications = self._code_modification_count(history)
            exact_stage = (
                current.managed.stage
                if current
                else "merged"
                if merged
                else "checked-head-merge"
                if latest_work
                and latest_work.managed.stage == Stage.CODE_REVIEW.value
                and latest_work.task.status == "done"
                else "reconciling"
            )
            phase = (
                "merged"
                if merged
                else PHASE_FOR_STAGE[Stage(current.managed.stage)].value
                if current and current.managed.purpose == "work"
                else "exception"
                if current
                else "code"
                if exact_stage == "checked-head-merge"
                else "reconciling"
            )
            control = self._run_control(run_key)
            return {
                "run_key": run_key,
                "control": control,
                "phase": phase,
                "stage": exact_stage,
                "active_card": (
                    {
                        "id": current.task.id,
                        "stage": current.managed.stage,
                        "iteration": current.managed.iteration,
                        "mode": (
                            current_record.mode.value if current_record else None
                        ),
                        "agent": current.task.assignee,
                        "status": current.task.status,
                        "purpose": current.managed.purpose,
                    }
                    if current
                    else None
                ),
                "attempts": {stage.value: count for stage, count in attempts.items()},
                "review_attempts": review_attempts,
                "review_remaining": review_remaining,
                "code_modifications": {
                    "used": code_modifications,
                    "remaining": max(
                        0,
                        self.config.code_modification_limit - code_modifications,
                    ),
                    "limit": self.config.code_modification_limit,
                },
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
                "frozen_artifacts": frozen_artifacts,
                "key_decisions": key_decisions,
                "residual_risks": residual_risks,
                "blocked": blocked_comment,
                "merge_blocker": merge_blocker,
                "board": run.workspace.board,
                "worktree": run.workspace.worktree,
                "repository_base_sha": run.workspace.repository_base_sha,
            }

    def status_summary(self, run_key: str) -> dict:
        """Return the authoritative local workflow snapshot without GitLab I/O."""
        history, run = self._history(run_key)
        active = [item for item in history if item.task.status in ACTIVE_STATUSES]
        current = active[-1] if active else None
        current_record = (
            parse_card_body(current.task.body)
            if current and current.managed.purpose == "work"
            else None
        )
        attempts = self._attempts_by_stage(history)
        document_review_attempts = self._review_attempts_by_stage(history)
        protocol_failures = self._protocol_failures_by_stage(history)
        review_stages = {
            Phase.SPEC: Stage.SPEC_REVIEW,
            Phase.PLAN: Stage.PLAN_REVIEW,
            Phase.TASKS: Stage.TASKS_REVIEW,
        }
        review_attempts = {
            phase.value: document_review_attempts.get(stage, 0)
            for phase, stage in review_stages.items()
        }
        review_remaining = {
            phase: max(0, self.config.document_review_limit - count)
            for phase, count in review_attempts.items()
        }
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

        merge_wait = (
            self.store.merge_wait(run_key)
            if hasattr(self.store, "merge_wait")
            else None
        )
        exact_stage = (
            current.managed.stage
            if current
            else "merge-wait"
            if merge_wait
            else "reconciling"
        )
        phase = (
            PHASE_FOR_STAGE[Stage(current.managed.stage)].value
            if current and current.managed.purpose == "work"
            else "exception"
            if current
            else Phase.CODE.value
            if merge_wait
            else "reconciling"
        )
        code_modifications = self._code_modification_count(history)
        store_health = self.store.health()
        controller_cursor = int(
            store_health["event_cursors"].get(run.workspace.board, 0)
        )
        kanban_max_event_id = self.reader.max_event_id(run.workspace.board)

        return {
            "run_key": run_key,
            "control": self._run_control(run_key),
            "notification_level": self.config.notification_level.value,
            "phase": phase,
            "stage": exact_stage,
            "active_card": (
                {
                    "id": current.task.id,
                    "stage": current.managed.stage,
                    "iteration": current.managed.iteration,
                    "mode": current_record.mode.value if current_record else None,
                    "agent": current.task.assignee,
                    "status": current.task.status,
                    "purpose": current.managed.purpose,
                }
                if current
                else None
            ),
            "attempts": {stage.value: count for stage, count in attempts.items()},
            "review_attempts": review_attempts,
            "review_remaining": review_remaining,
            "code_modifications": {
                "used": code_modifications,
                "remaining": max(
                    0,
                    self.config.code_modification_limit - code_modifications,
                ),
                "limit": self.config.code_modification_limit,
            },
            "protocol_failures": {
                stage.value: count for stage, count in protocol_failures.items()
            },
            "blocked": blocked_comment,
            "merge_wait": merge_wait,
            "worker_runtime": (
                self.store.runtime_for_run(run_key)
                if hasattr(self.store, "runtime_for_run")
                else []
            ),
            "board": run.workspace.board,
            "worktree": run.workspace.worktree,
            "repository_base_sha": run.workspace.repository_base_sha,
            "snapshot": {
                "authority": "controller-store+kanban",
                "gitlab_audit": "not_requested",
                "controller_event_cursor": controller_cursor,
                "kanban_max_event_id": kanban_max_event_id,
                "event_lag": max(0, kanban_max_event_id - controller_cursor),
                "event_lag_warning": (
                    max(0, kanban_max_event_id - controller_cursor)
                    >= self.config.event_lag_warning_threshold
                ),
                "outbox_pending": store_health["outbox_pending"],
                "failed_operations": store_health["failed_operations"],
                "dependency_outages": store_health.get(
                    "dependency_outages",
                    [],
                ),
            },
        }

    def preflight(self) -> dict:
        checks: dict[str, dict] = {}
        for name, command in {
            "hermes": self.config.hermes_command,
            "glab": self.config.glab_command,
            "lark": self.config.lark_command,
            "git": "git",
        }.items():
            resolved = shutil.which(command)
            checks[name] = {"ok": resolved is not None, "path": resolved}
        try:
            self.config.read_token()
            checks["gitlab_token"] = {"ok": True, "mode": "private-file"}
        except Exception as exc:  # noqa: BLE001 - structured preflight result
            checks["gitlab_token"] = {
                "ok": False,
                "error": self._error_text(exc),
            }
        checks["state_dir"] = {
            "ok": self.config.state_dir.is_dir()
            and self.config.state_dir.stat().st_mode & 0o200 != 0,
            "path": str(self.config.state_dir),
        }
        checks["profiles_root"] = {
            "ok": self.config.profiles_root.is_dir(),
            "path": str(self.config.profiles_root),
        }
        return {
            "ok": all(item["ok"] for item in checks.values()),
            "checks": checks,
        }

    def validate_completion(self, raw: dict) -> dict:
        request = CompletionValidationRequest.model_validate(raw)
        matches = [
            card
            for run_key in self.store.run_keys()
            for card in self.store.cards_for_run(run_key)
            if card.card_id == request.card_id and card.purpose == "work"
        ]
        if len(matches) != 1:
            raise ValueError(
                "card_id must identify exactly one managed Hollysys work card"
            )
        managed = matches[0]
        history, run = self._history(managed.run_key)
        item = next(
            entry for entry in history if entry.task.id == request.card_id
        )
        metadata = CompletionMetadata.model_validate(request.metadata)
        self._validate_completion_context(run, item, metadata)
        self._validate_finalization_context(history, item, metadata)
        if metadata.repository_evidence is not None:
            self.gitlab.validate_repository_evidence(run, metadata)
        if metadata.stage in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
            Stage.TEST,
            Stage.CODE_REVIEW,
        }:
            self.gitlab.validate_gate(run, metadata)
        return {
            "ok": True,
            "run_key": run.run_key,
            "card_id": item.task.id,
            "stage": metadata.stage.value,
            "iteration": metadata.iteration,
            "context": "controller-verified",
        }

    def abort_request(self, raw: dict) -> dict:
        request = AbortRequest.model_validate(raw)
        key = f"abort-request:{request.message_id}"
        previous = self.store.begin_request(
            key, "abort-request", request.model_dump(mode="json")
        )
        if previous is not None:
            return previous
        try:
            _, run = self._history(request.run_key)
            if (
                request.sender != run.origin.initiator_open_id
                and request.sender not in self.config.abort_admin_open_ids
            ):
                raise PermissionError(
                    "only the run initiator or configured abort administrator "
                    "may abort this run"
                )
            control = self.store.run_control(request.run_key)
            if control and control["state"] != "active":
                raise ValueError(
                    f"run is already {control['state']}"
                )
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            token = "".join(secrets.choice(alphabet) for _ in range(8))
            token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
            expires_at = (
                int(time.time()) + self.config.abort_confirmation_ttl_seconds
            )
            created = self.store.create_abort_request(
                request_id=key,
                run_key=request.run_key,
                token_hash=token_hash,
                sender=request.sender,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
                reason=request.reason,
                expires_at=expires_at,
            )
            response = {
                **created,
                "confirmation_token": token,
                "confirmation_command": (
                    f"确认废止 {request.run_key} {token}"
                ),
                "impact": {
                    "active_cards": [
                        item.task.id
                        for item in self._history(request.run_key)[0]
                        if item.task.status in ACTIVE_STATUSES
                    ],
                    "mr_action": "close-if-open",
                    "branch_worktree": "preserve",
                },
            }
            persisted_response = dict(response)
            persisted_response.pop("confirmation_token", None)
            persisted_response.pop("confirmation_command", None)
            persisted_response["confirmation_required"] = True
            self.store.finish_request(key, persisted_response)
            return response
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise

    def abort_confirm(self, raw: dict) -> dict:
        request = AbortConfirmRequest.model_validate(raw)
        key = f"abort-confirm:{request.message_id}"
        request_payload = request.model_dump(
            mode="json",
            exclude={"token"},
        )
        request_payload["token_hash"] = hashlib.sha256(
            request.token.encode("ascii")
        ).hexdigest()
        previous = self.store.begin_request(
            key, "abort-confirm", request_payload
        )
        if previous is not None:
            return previous
        try:
            control = self.store.confirm_abort_request(
                run_key=request.run_key,
                token_hash=hashlib.sha256(
                    request.token.encode("ascii")
                ).hexdigest(),
                sender=request.sender,
                chat_id=request.chat_id,
                thread_id=request.thread_id,
                message_id=request.message_id,
            )
            continuation_error: str | None = None
            try:
                with self._lock:
                    self._continue_abort(request.run_key)
            except Exception as exc:  # noqa: BLE001 - durable retry boundary
                continuation_error = self._error_text(exc)
                self.last_reconcile_error = continuation_error
                self._enqueue_controller_failure(request.run_key, exc)
            result = self.store.run_control(request.run_key) or control
            response = {
                "run_key": request.run_key,
                "state": result["state"],
                "reason": result.get("abort_reason"),
                "continuation": (
                    "pending-retry" if continuation_error else "finished"
                ),
            }
            if continuation_error:
                response["continuation_error"] = continuation_error
            self.store.finish_request(key, response)
            return response
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise

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
                block_fields = self._human_block_fields(block_comment)
                if not self._valid_human_block(block_fields):
                    raise ValueError(
                        "human block is not an allowed v3 technical/safety block"
                    )
                stage = Stage(managed.stage)
                original_record = parse_card_body(task.body)
                retry = self._resolved_retry(
                    history,
                    task.id,
                    stage,
                    original_record.mode,
                    request.answer,
                )
                if retry is None:
                    if task.status not in RESOLVABLE_HUMAN_BLOCK_STATUSES:
                        raise ValueError(
                            "card is not currently a resolvable human block"
                        )
                    retry = self._create_work(
                        run,
                        stage,
                        task.id,
                        mode=original_record.mode,
                        repair_context=original_record.repair_context,
                        resume_answer=request.answer,
                        resumed_from=task.id,
                        publish=False,
                    )
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
                if task.status in {"triage", "todo"}:
                    self.kanban.prepare_human_block_for_completion(
                        run.workspace.board, task.id
                    )
                if task.status in RESOLVABLE_HUMAN_BLOCK_STATUSES:
                    cancelled = self._controller_completion(
                        run,
                        managed,
                        task.id,
                        outcome=Outcome.CANCELLED,
                        mode=original_record.mode,
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
                if retry.status in ACTIVE_STATUSES:
                    retry = self._ensure_work_published(run, retry)
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
                    if managed:
                        self._record_agent_lifecycle_event(managed, event)
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
                    elif request["kind"] == "abort-request":
                        self.abort_request(request["payload"])
                    elif request["kind"] == "abort-confirm":
                        self.abort_confirm(request["payload"])
                for run_key in self.store.active_abort_run_keys():
                    self._continue_abort(run_key)
                self._enqueue_stale_worker_notices()
                if self._dependency_retry_blocked("gitlab"):
                    self.last_reconcile_at = int(time.time())
                    self.last_reconcile_error = "gitlab circuit is in backoff"
                    self.flush_outbox()
                    return
                run_errors: list[tuple[str, Exception]] = []
                active_run_keys = [
                    run_key
                    for run_key in self.store.run_keys()
                    if (
                        self.store.run_control(run_key) is None
                        or self.store.run_control(run_key)["state"] == "active"
                    )
                ]
                for run_key in active_run_keys:
                    try:
                        self.reconcile_run(run_key)
                    except Exception as exc:  # noqa: BLE001 - isolate each run
                        run_errors.append((run_key, exc))
                        # GitLab is shared by all runs. One confirmed failure
                        # opens the circuit instead of multiplying requests by
                        # the number of active runs.
                        break
                if run_errors:
                    summary = "; ".join(
                        f"{run_key}: {self._error_text(error)}"
                        for run_key, error in run_errors
                    )
                    error_class = self._dependency_error_class(
                        run_errors[0][1]
                    )
                    outage = self.store.record_dependency_failure(
                        "gitlab",
                        summary,
                        initial_backoff_seconds=(
                            self.config.dependency_backoff_initial_seconds
                        ),
                        maximum_backoff_seconds=(
                            self.config.dependency_backoff_max_seconds
                        ),
                        error_class=error_class,
                    )
                    for run_key in active_run_keys:
                        self.store.associate_outage_run(
                            "gitlab",
                            str(outage["outage_id"]),
                            run_key,
                        )
                        self._enqueue_dependency_outage(run_key, outage)
                    self.last_reconcile_at = int(time.time())
                    self.last_reconcile_error = summary
                    self.flush_outbox()
                    return
                recovered = self.store.recover_dependency("gitlab")
                if recovered is not None:
                    for run_key in self.store.outage_run_keys(
                        "gitlab",
                        str(recovered["outage_id"]),
                    ):
                        self._enqueue_dependency_recovered(run_key, recovered)
                self.last_reconcile_at = int(time.time())
                self.last_reconcile_error = None
                self.flush_outbox()
            except Exception as exc:
                self.last_reconcile_error = str(exc)
                raise

    def reconcile_run(self, run_key: str) -> None:
        control = self._run_control(run_key)
        if control and control["state"] in {
            "abort_requested",
            "aborting",
        }:
            self._continue_abort(run_key)
            return
        if control and control["state"] in {
            "aborted",
            "completed_before_abort",
        }:
            return
        if self._dependency_retry_blocked("gitlab"):
            return
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
            if item.task.status == "blocked":
                failure_fuse = any(
                    kind in {"gave_up", "spawn_auto_blocked"}
                    for kind in item.task.event_kinds
                )
                human_block_comment = next(
                    (
                        str(comment["body"])
                        for comment in reversed(item.task.comments)
                        if "[human-block:v1]" in str(comment["body"])
                    ),
                    None,
                )
                if item.task.latest_outcome is None and not failure_fuse:
                    # This is an interrupted controller publish, not a worker
                    # block. Initial status events vary by Hermes version, so
                    # the absence of a task-run outcome is the stable signal.
                    self.kanban.release(run.workspace.board, item.task.id)
                    return
                if (
                    item.task.latest_outcome == "blocked"
                    and human_block_comment is not None
                ):
                    block_fields = self._human_block_fields(
                        human_block_comment
                    )
                    missing = REQUIRED_HUMAN_BLOCK_FIELDS - block_fields.keys()
                    if not self._valid_human_block(block_fields):
                        reason = (
                            "unsupported human block; business ambiguity must "
                            "be resolved autonomously"
                        )
                        if missing:
                            reason += "; missing=" + ",".join(sorted(missing))
                        if not any(
                            "[controller-block-rejected:v3]"
                            in str(comment["body"])
                            for comment in item.task.comments
                        ):
                            self.kanban.comment(
                                run.workspace.board,
                                item.task.id,
                                "[controller-block-rejected:v3]\n"
                                f"reason: {reason}",
                                "hollysys-controller",
                            )
                        self.kanban.release(
                            run.workspace.board, item.task.id
                        )
                        return
                    self._enqueue_human_block(run, item, human_block_comment)
                if (
                    item.task.latest_outcome == "blocked"
                    and human_block_comment is None
                ):
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
            metadata = validate_persisted_completion_metadata(
                latest.task.latest_metadata
            )
            self._validate_completion_context(run, latest, metadata)
            self._validate_finalization_context(history, latest, metadata)
        except (ValidationError, ValueError, TypeError) as exc:
            self._protocol_failure(run, history, latest, self._error_text(exc))
            return
        self._enqueue_agent_completed(run, latest, metadata)

        if metadata.repository_evidence is not None:
            try:
                self.gitlab.validate_repository_evidence(run, metadata)
            except ValueError as exc:
                self._protocol_failure(run, history, latest, str(exc))
                return

        gate_stages = {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
            Stage.TEST,
            Stage.CODE_REVIEW,
        }
        if metadata.stage in gate_stages and metadata.outcome in {
            Outcome.PASS,
            Outcome.FAIL,
        }:
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
        if metadata.mode == WorkMode.FINALIZATION:
            try:
                self.gitlab.validate_artifact_completion(run, metadata)
            except ValueError as exc:
                self._protocol_failure(run, history, latest, str(exc))
                return

        live_mr = self.gitlab.delivery_mr(run, metadata.mr_iid)
        if live_mr is not None and live_mr.get("sha"):
            current_head = str(live_mr["sha"])
            violation = self._frozen_violation(run, history, current_head)
            if violation is not None:
                phase = PHASE_FOR_STAGE[metadata.stage]
                repair_mode = (
                    WorkMode.FINALIZATION
                    if metadata.mode == WorkMode.FINALIZATION
                    else WorkMode.NORMAL
                )
                self._create_frozen_repair(
                    run,
                    history,
                    phase,
                    latest.task.id,
                    violation,
                    mode=repair_mode,
                )
                return
            if (
                metadata.stage
                in {
                    Stage.SPEC_REVIEW,
                    Stage.PLAN_REVIEW,
                    Stage.TASKS_REVIEW,
                }
                and metadata.outcome == Outcome.PASS
            ):
                try:
                    self.gitlab.validate_artifact_gate_at_ref(
                        run, metadata, current_head
                    )
                except ValueError as exc:
                    self._create_review_repair(
                        run,
                        history,
                        metadata,
                        latest.task.id,
                        [str(exc)],
                    )
                    return
            if metadata.mode == WorkMode.FINALIZATION:
                try:
                    self.gitlab.validate_artifact_gate_at_ref(
                        run, metadata, current_head
                    )
                except ValueError as exc:
                    phase = PHASE_FOR_STAGE[metadata.stage]
                    self._create_frozen_repair(
                        run,
                        history,
                        phase,
                        latest.task.id,
                        str(exc),
                        mode=WorkMode.FINALIZATION,
                    )
                    return

        review_attempts = self._review_attempts_by_stage(history)
        code_modifications = self._code_modification_count(history)
        paired_test = None
        if metadata.stage == Stage.CODE_REVIEW and metadata.outcome in {
            Outcome.PASS,
            Outcome.FAIL,
        }:
            paired_test = self._latest_valid_completion(
                history,
                Stage.TEST,
                {Outcome.PASS, Outcome.FAIL},
            )
            if (
                paired_test is None
                or paired_test.mr_iid != metadata.mr_iid
                or paired_test.mr_url != metadata.mr_url
                or paired_test.head_sha != metadata.head_sha
            ):
                self._create_work(run, Stage.TEST, latest.task.id)
                return
            try:
                self.gitlab.validate_gate(run, paired_test)
            except ValueError:
                self._create_work(run, Stage.TEST, latest.task.id)
                return
        route = route_completion(
            metadata,
            review_attempts_by_stage=review_attempts,
            config=self.config,
            paired_test=paired_test,
            code_modifications=code_modifications,
        )
        if route.blocked_reason:
            reason = route.blocked_reason
            if metadata.stage == Stage.CODE_REVIEW and paired_test is not None:
                findings = self._code_gate_issues(paired_test, metadata)
                reason += (
                    f"; head={metadata.head_sha}; "
                    f"tester={paired_test.outcome.value}; "
                    f"code-reviewer={metadata.outcome.value}; findings="
                    + " | ".join(findings[:6])
                    + "; required_action=human must decide whether to continue "
                    "with a new modification budget or stop delivery"
                )
            self._exception(run, latest.task.id, reason)
            return
        if route.next_stage:
            repair_context = None
            if (
                metadata.stage == Stage.TEST
                and metadata.test_disposition
                == TestDisposition.SKIPPED_UNAVAILABLE
            ):
                self._enqueue_test_skipped(run, metadata)
            if metadata.stage in {
                Stage.SPEC_REVIEW,
                Stage.PLAN_REVIEW,
                Stage.TASKS_REVIEW,
            } and metadata.outcome == Outcome.FAIL:
                review_attempt = review_attempts.get(metadata.stage, 0)
                repair_context = RepairContext(
                    kind=RepairKind.REVIEW_FAILURE,
                    trigger_card_id=latest.task.id,
                    issues=metadata.issues,
                    review_attempt=review_attempt,
                    review_limit=self.config.document_review_limit,
                )
                self._enqueue_review_failed(
                    run,
                    metadata,
                    review_attempt,
                    route.next_mode,
                )
            if (
                metadata.stage == Stage.CODE_REVIEW
                and paired_test is not None
                and (
                    paired_test.outcome == Outcome.FAIL
                    or metadata.outcome == Outcome.FAIL
                )
            ):
                next_modification = code_modifications + 1
                repair_context = RepairContext(
                    kind=RepairKind.CODE_GATE_FAILURE,
                    trigger_card_id=latest.task.id,
                    related_card_ids=[
                        paired_test.kanban_card_id,
                        metadata.kanban_card_id,
                    ],
                    head_sha=metadata.head_sha,
                    code_modification=next_modification,
                    code_modification_limit=self.config.code_modification_limit,
                    issues=self._code_gate_issues(paired_test, metadata),
                )
                self._enqueue_code_retry(
                    run,
                    paired_test,
                    metadata,
                    next_modification,
                )
            if metadata.stage in {
                Stage.SPEC_REVIEW,
                Stage.PLAN_REVIEW,
                Stage.TASKS_REVIEW,
            } and metadata.outcome == Outcome.PASS:
                self._enqueue_phase_frozen(
                    run,
                    PHASE_FOR_STAGE[metadata.stage],
                    metadata,
                )
            if metadata.mode == WorkMode.FINALIZATION:
                self._enqueue_phase_frozen(
                    run,
                    PHASE_FOR_STAGE[metadata.stage],
                    metadata,
                )
            created = self._create_work(
                run,
                route.next_stage,
                latest.task.id,
                mode=route.next_mode,
                repair_context=repair_context,
            )
            if PHASE_FOR_STAGE[route.next_stage] != PHASE_FOR_STAGE[metadata.stage]:
                self._enqueue_phase_started(
                    run, PHASE_FOR_STAGE[route.next_stage], created
                )
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
            violation = self._frozen_violation(run, history, current_head)
            if violation is not None:
                self._create_frozen_repair(
                    run,
                    history,
                    Phase.CODE,
                    latest.task.id,
                    violation,
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
                    self.store.clear_merge_wait(run.run_key)
                    self._create_work(run, Stage.TEST, latest.task.id)
                else:
                    waiting = self.store.set_merge_wait(
                        run.run_key,
                        mr_iid=int(mr["iid"]),
                        head_sha=current_head,
                        blocker_kind=self._merge_blocker_kind(exc),
                        blocker=self._error_text(exc),
                        retry_seconds=self.config.merge_wait_retry_seconds,
                    )
                    if waiting["changed"]:
                        self._enqueue_progress(
                            run,
                            (
                                "merge-wait:"
                                f"{waiting['blocker_kind']}:{current_head}"
                            ),
                            self._mention(run.origin)
                            + "代码门禁已完成，但合并条件尚未满足。\n"
                            f"run={run.run_key} stage=merge-wait "
                            f"blocker={waiting['blocker_kind']} "
                            f"mr={mr.get('web_url')} head={current_head} "
                            f"next_retry_at={waiting['next_retry_at']}",
                        )
                    if (
                        int(time.time()) - int(waiting["first_seen_at"])
                        >= self.config.merge_wait_timeout_seconds
                    ):
                        self.store.enqueue(
                            (
                                f"{run.run_key}:merge-wait-timeout:"
                                f"{waiting['blocker_kind']}:{current_head}"
                            ),
                            run.run_key,
                            "merge-wait-timeout",
                            {
                                "origin": run.origin.model_dump(mode="json"),
                                "text": self._mention(run.origin)
                                + "合并等待已超过时限，需要人类处理外部门禁。\n"
                                f"run={run.run_key} "
                                f"blocker={waiting['blocker_kind']} "
                                f"mr={mr.get('web_url')} head={current_head}\n"
                                "修复 Draft/pipeline/discussion/approval 后 "
                                "Controller 会按 checked head 自动续跑；也可在飞书废止流程。",
                            },
                        )
                # Pipeline/discussion/MR readiness is an external fact. Leave
                # the run cardless and let the 30-second reconcile retry it.
                return
            self.store.clear_merge_wait(run.run_key)
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

    def flush_outbox(self) -> None:
        for item in self.store.pending_outbox():
            try:
                payload = json.loads(item["payload"])
                origin = FeishuOrigin.model_validate(payload["origin"])
                self.notifier.send(item["outbox_key"], origin, payload["text"])
                self.store.finish_outbox(item["outbox_key"])
            except Exception as exc:  # noqa: BLE001 - durable outbox boundary
                self.store.fail_outbox(item["outbox_key"], str(exc))

    def _continue_abort(self, run_key: str) -> None:
        control = self.store.run_control(run_key)
        if control is None or control["state"] not in {
            "abort_requested",
            "aborting",
        }:
            return
        history, run = self._history(run_key)
        self.store.mark_aborting(run_key)
        reason = str(control.get("abort_reason") or "human requested abort")
        for item in history:
            if (
                item.managed.purpose in {"root", "work", "exception"}
                and item.task.status in ACTIVE_STATUSES
            ):
                self.kanban.abort_task(
                    run.workspace.board,
                    item.task.id,
                    f"run aborted by human: {reason}",
                )
        mr = self.gitlab.abort_delivery(
            run,
            requested_by=str(control.get("abort_requested_by") or "unknown"),
            reason=reason,
        )
        terminal = (
            "completed_before_abort"
            if mr.get("state") == "merged"
            else "aborted"
        )
        self.store.finish_abort(run_key, terminal)
        self.store.enqueue(
            f"{run_key}:aborted:{terminal}",
            run_key,
            "aborted",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + (
                    "废止请求到达前交付已经完成合并，未关闭已合并 MR。\n"
                    if terminal == "completed_before_abort"
                    else "自动交付已按人类确认废止。\n"
                )
                + f"run={run_key} requested_by="
                f"{control.get('abort_requested_by')} reason={reason[:500]}\n"
                f"mr={mr.get('web_url')} state={mr.get('state')} "
                "branch_worktree=preserved",
            },
        )
        self.flush_outbox()

    def _run_control(self, run_key: str) -> dict:
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "run_control"):
            return {"run_key": run_key, "state": "active"}
        return store.run_control(run_key) or {
            "run_key": run_key,
            "state": "active",
        }

    def _dependency_retry_blocked(self, dependency: str) -> bool:
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "open_dependency_outages"):
            return False
        now = int(time.time())
        return any(
            outage["dependency"] == dependency
            and int(outage["next_retry_at"]) > now
            for outage in store.open_dependency_outages()
        )

    def _record_agent_lifecycle_event(
        self, managed: ManagedCard, event: EventRecord
    ) -> None:
        if managed.purpose != "work":
            return
        worker_session_id = None
        if isinstance(event.payload, dict):
            candidate = event.payload.get("worker_session_id")
            if isinstance(candidate, str) and candidate.strip():
                worker_session_id = candidate
        if hasattr(self.store, "record_card_runtime_event"):
            self.store.record_card_runtime_event(
                board=managed.board,
                card_id=managed.card_id,
                kind=event.kind,
                created_at=event.created_at,
                worker_session_id=worker_session_id,
                lease_seconds=self.config.worker_progress_lease_seconds,
            )
        try:
            _, run = self._history(managed.run_key)
            task = self.reader.task(managed.board, managed.card_id)
        except Exception:  # noqa: BLE001 - lifecycle notices are best effort
            return
        if task is None:
            return
        if event.kind in {"claimed", "started", "worker_started"}:
            if self.config.notification_level != NotificationLevel.VERBOSE:
                return
            self._enqueue_progress(
                run,
                f"agent:{managed.card_id}:started:{event.run_id or event.id}",
                self._mention(run.origin)
                + "Agent 已开始工作。\n"
                f"run={run.run_key} stage={managed.stage} "
                f"iteration={managed.iteration} agent={task.assignee} "
                f"card={task.id}",
            )
        elif event.kind in {
            "blocked",
            "crashed",
            "timed_out",
            "gave_up",
            "spawn_auto_blocked",
        }:
            if self.config.notification_level == NotificationLevel.MINIMAL and (
                event.kind not in {"blocked", "gave_up", "spawn_auto_blocked"}
            ):
                return
            self._enqueue_progress(
                run,
                f"agent:{managed.card_id}:interrupted:{event.id}",
                self._mention(run.origin)
                + "Agent 工作已中断。\n"
                f"run={run.run_key} stage={managed.stage} "
                f"iteration={managed.iteration} agent={task.assignee} "
                f"card={task.id} event={event.kind}",
            )

    def _enqueue_stale_worker_notices(self) -> None:
        if not hasattr(self.store, "runtime_for_run"):
            return
        now = int(time.time())
        for run_key in self.store.run_keys():
            control = self.store.run_control(run_key)
            if control and control["state"] != "active":
                continue
            for runtime in self.store.runtime_for_run(run_key):
                deadline = runtime.get("deadline_at")
                if deadline is None or int(deadline) >= now:
                    continue
                task = self.reader.task(
                    str(runtime["board"]),
                    str(runtime["card_id"]),
                )
                if task is None or task.status != "running":
                    continue
                try:
                    _, run = self._history(run_key)
                except Exception as exc:  # noqa: BLE001 - best-effort notice
                    LOG.warning(
                        "worker lease evidence lookup failed for run %s: %s",
                        run_key,
                        self._error_text(exc),
                    )
                    continue
                self.store.enqueue(
                    f"{run_key}:worker-lease:{runtime['card_id']}:{deadline}",
                    run_key,
                    "worker-lease-expired",
                    {
                        "origin": run.origin.model_dump(mode="json"),
                        "text": self._mention(run.origin)
                        + "Agent 仍显示 running，但超过进展租约，需要检查运行证据。\n"
                        f"run={run_key} stage={runtime['stage']} "
                        f"card={runtime['card_id']} "
                        f"last_event={runtime['last_event_kind']} "
                        f"deadline_at={deadline}\n"
                        "Controller 不会仅按总运行时长终止 Agent；请检查 session/PID/"
                        "worktree 后再决定是否废止流程。",
                    },
                )

    def _enqueue_agent_completed(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        if (
            self.config.notification_level != NotificationLevel.VERBOSE
            or not hasattr(self, "store")
        ):
            return
        duration = (
            max(0, item.task.completed_at - item.task.created_at)
            if item.task.completed_at is not None
            else None
        )
        self._enqueue_progress(
            run,
            f"agent:{item.task.id}:completed:{metadata.outcome.value}",
            self._mention(run.origin)
            + "Agent 已完成工作，Controller 已接受交付协议。\n"
            f"run={run.run_key} stage={metadata.stage.value} "
            f"iteration={metadata.iteration} agent={item.task.assignee} "
            f"card={item.task.id} outcome={metadata.outcome.value} "
            f"duration_seconds={duration if duration is not None else 'unknown'}",
        )

    def health(self, probe: str = "readiness") -> dict:
        if probe not in {"liveness", "readiness"}:
            raise ValueError(f"unsupported health probe {probe}")
        data = self.store.health()
        for outage in data["dependency_outages"]:
            outage["circuit_open"] = (
                int(outage["failures"])
                >= self.config.dependency_circuit_failure_threshold
            )
        data["liveness"] = {
            "ok": True,
            "store": True,
            "rpc": True,
        }
        if probe == "liveness":
            data["probe"] = probe
            data["status"] = "alive"
            data["ok"] = True
            return data
        now = int(time.time())
        try:
            boards = self.reader.discover_boards()
            discovery_error = None
        except Exception as exc:  # noqa: BLE001 - readiness reports degradation
            boards = []
            discovery_error = self._error_text(exc)
        board_health: dict[str, dict] = {}
        for board in sorted(boards):
            try:
                board_health[board] = {
                    "ok": True,
                    "max_event_id": self.reader.max_event_id(board),
                }
            except Exception as exc:  # noqa: BLE001 - health reports degradation
                board_health[board] = {"ok": False, "error": str(exc)}
        kanban_ok = discovery_error is None and all(
            item["ok"] for item in board_health.values()
        ) and (
            bool(boards) or not self.store.run_keys()
        )
        reconcile_fresh = (
            self.last_reconcile_at is not None
            and now - self.last_reconcile_at <= self.config.health_stale_seconds
        )
        data.update(
            {
                "probe": probe,
                "boards": sorted(boards),
                "board_discovery_error": discovery_error,
                "board_health": board_health,
                "last_reconcile_at": self.last_reconcile_at,
                "last_reconcile_error": self.last_reconcile_error,
                "reconcile_fresh": reconcile_fresh,
                "kanban_ok": kanban_ok,
            }
        )
        readiness_ok = (
            self.last_reconcile_error is None
            and reconcile_fresh
            and kanban_ok
            and data["outbox_pending"] < self.config.outbox_warning_threshold
            and not data["dependency_outages"]
        )
        try:
            data["gitlab"] = self.gitlab.health()
            readiness_ok = readiness_ok and bool(data["gitlab"]["ok"])
        except Exception as exc:  # noqa: BLE001 - health must return degraded state
            data["gitlab"] = {"ok": False, "error": str(exc)}
            readiness_ok = False
        data["readiness"] = {"ok": readiness_ok}
        data["status"] = (
            "healthy"
            if readiness_ok
            else "degraded"
            if data["liveness"]["ok"]
            else "unhealthy"
        )
        data["ok"] = readiness_ok
        return data

    def _create_work(
        self,
        run: RunRecord,
        stage: Stage,
        parent_card_id: str,
        *,
        mode: WorkMode = WorkMode.NORMAL,
        repair_context: RepairContext | None = None,
        resume_answer: str | None = None,
        resumed_from: str | None = None,
        publish: bool = True,
    ) -> TaskRecord:
        full_history, _ = self._history(run.run_key)
        frozen_baselines = self._frozen_baselines(full_history, run)
        mr = self.gitlab.delivery_mr(run)
        frozen_repair = (
            repair_context is not None
            and repair_context.kind == RepairKind.FROZEN_ARTIFACT_VIOLATION
        )
        if mr is not None and mr.get("sha") and not frozen_repair:
            violation = self._frozen_violation(
                run, full_history, str(mr["sha"])
            )
            if violation is not None:
                phase = PHASE_FOR_STAGE[stage]
                stage = PRODUCER_FOR_PHASE[phase]
                if mode == WorkMode.FINALIZATION and repair_context is not None:
                    repair_context = repair_context.model_copy(
                        update={
                            "issues": [
                                *repair_context.issues,
                                "恢复冻结工件后再完成 finalization。 " + violation,
                            ]
                        }
                    )
                else:
                    prior_issues = (
                        repair_context.issues
                        if repair_context is not None
                        else []
                    )
                    mode = WorkMode.NORMAL
                    repair_context = RepairContext(
                        kind=RepairKind.FROZEN_ARTIFACT_VIOLATION,
                        trigger_card_id=parent_card_id,
                        issues=[
                            *prior_issues,
                            "恢复冻结工件到 Controller 提供的基线；"
                            "只在当前阶段吸收必要适配。 " + violation,
                        ],
                    )
                self._enqueue_progress(
                    run,
                    f"{phase.value}:preflight-frozen-repair:"
                    f"{hashlib.sha256(violation.encode()).hexdigest()[:12]}",
                    self._mention(run.origin)
                    + f"释放下一张卡前发现冻结工件被修改，"
                    f"已改派 {phase.value.upper()} 恢复任务。\n"
                    f"run={run.run_key} reason={violation[:500]}",
                )
        if repair_context is not None:
            repair_context = repair_context.model_copy(
                update={"frozen_baselines": frozen_baselines}
            )
        history = self.store.cards_for_run(run.run_key)
        attempts = sum(
            1
            for card in history
            if card.purpose == "work" and card.stage == stage.value
        )
        iteration = attempts + 1
        key = f"{run.run_key}:{stage.value}:{iteration}:{mode.value}:work"
        assignee = self.config.stage_assignees[stage]
        skills = self.config.stage_skills[stage]
        record = CardRecord(
            run=run,
            stage=stage,
            iteration=iteration,
            mode=mode,
            idempotency_key=key,
            parent_card_id=parent_card_id,
            assignee=assignee,
            skills=skills,
            frozen_baselines=frozen_baselines,
            repair_context=repair_context,
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
        return self._ensure_work_published(run, task) if publish else task

    def _ensure_work_published(self, run: RunRecord, task: TaskRecord) -> TaskRecord:
        record = parse_card_body(task.body)
        key = record.idempotency_key
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
        mode: WorkMode,
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
                and record.mode == mode
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
                "[controller-protocol-error:v3]" in str(comment["body"])
                for comment in item.task.comments
            ):
                continue
            result[stage] = result.get(stage, 0) + 1
        return result

    def _review_attempts_by_stage(
        self, history: list[HistoryItem]
    ) -> dict[Stage, int]:
        review_stages = {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
        }
        review_stage_values = {stage.value for stage in review_stages}
        result: dict[Stage, int] = {}
        for item in history:
            if (
                item.managed.purpose != "work"
                or item.managed.stage not in review_stage_values
                or item.task.status != "done"
                or any(
                    "[controller-protocol-error:v3]" in str(comment["body"])
                    for comment in item.task.comments
                )
            ):
                continue
            try:
                metadata = validate_persisted_completion_metadata(
                    item.task.latest_metadata
                )
                self._validate_completion_context(
                    parse_card_body(item.task.body).run,
                    item,
                    metadata,
                )
            except (ValidationError, ValueError, TypeError):
                continue
            if metadata.outcome not in {Outcome.PASS, Outcome.FAIL}:
                continue
            stage = Stage(item.managed.stage)
            result[stage] = result.get(stage, 0) + 1
        return result

    def _frozen_baselines(
        self, history: list[HistoryItem], run: RunRecord
    ) -> list[ArtifactBaseline]:
        root = next(
            item for item in history if item.managed.purpose == "root"
        )
        prd_digest = hashlib.sha256(
            f"{run.source.prd_path}\0{run.source.prd_blob_sha}\n".encode()
        ).hexdigest()
        baselines = [
            ArtifactBaseline(
                phase="prd",
                disposition=BaselineDisposition.SOURCE,
                artifact_paths=[run.source.prd_path],
                artifact_digest=prd_digest,
                artifact_commit_sha=run.source.prd_commit_sha,
                source_card_id=root.task.id,
            )
        ]
        phase_contracts = (
            ("spec", Stage.SPEC_WRITE, Stage.SPEC_REVIEW),
            ("plan", Stage.PLAN_WRITE, Stage.PLAN_REVIEW),
            ("tasks", Stage.TASKS_WRITE, Stage.TASKS_REVIEW),
        )
        for phase, producer, reviewer in phase_contracts:
            candidate: tuple[int, CompletionMetadata] | None = None
            for index, item in enumerate(history):
                if item.managed.purpose != "work" or item.task.status != "done":
                    continue
                if item.managed.stage not in {producer.value, reviewer.value}:
                    continue
                try:
                    metadata = validate_persisted_completion_metadata(
                        item.task.latest_metadata
                    )
                    self._validate_completion_context(run, item, metadata)
                except (ValidationError, ValueError, TypeError):
                    continue
                reviewed = (
                    metadata.stage == reviewer
                    and metadata.outcome == Outcome.PASS
                    and metadata.baseline_disposition
                    == BaselineDisposition.REVIEWED
                )
                forced = (
                    metadata.stage == producer
                    and metadata.mode == WorkMode.FINALIZATION
                    and metadata.outcome == Outcome.PASS
                    and metadata.baseline_disposition
                    == BaselineDisposition.FORCED_AFTER_REVIEW_LIMIT
                )
                if reviewed or forced:
                    candidate = (index, metadata)
            if candidate is None:
                break
            metadata = candidate[1]
            assert metadata.artifact_digest
            assert metadata.artifact_commit_sha
            decision_urls = list(metadata.gitlab_urls)
            if (
                metadata.forced_advance is not None
                and metadata.forced_advance.decision_url not in decision_urls
            ):
                decision_urls.append(metadata.forced_advance.decision_url)
            baselines.append(
                ArtifactBaseline(
                    phase=phase,
                    disposition=metadata.baseline_disposition,
                    artifact_paths=metadata.artifact_paths,
                    artifact_digest=metadata.artifact_digest,
                    artifact_commit_sha=metadata.artifact_commit_sha,
                    source_card_id=metadata.kanban_card_id,
                    decision_urls=decision_urls,
                    key_decisions=metadata.key_decisions,
                    unresolved_findings=(
                        metadata.forced_advance.unresolved_findings
                        if metadata.forced_advance is not None
                        else []
                    ),
                    residual_risk=metadata.residual_risk,
                )
            )
        return baselines

    def _frozen_violation(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        ref: str,
    ) -> str | None:
        for baseline in self._frozen_baselines(history, run):
            try:
                self.gitlab.validate_baseline_at_ref(run, baseline, ref)
            except ValueError as exc:
                return f"{baseline.phase}: {exc}"
        return None

    def _create_review_repair(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        metadata: CompletionMetadata,
        parent_card_id: str,
        issues: list[str],
    ) -> TaskRecord:
        review_attempt = self._review_attempts_by_stage(history).get(
            metadata.stage, 0
        )
        mode = (
            WorkMode.FINALIZATION
            if review_attempt >= self.config.document_review_limit
            else WorkMode.NORMAL
        )
        context = RepairContext(
            kind=RepairKind.REVIEW_FAILURE,
            trigger_card_id=parent_card_id,
            issues=issues,
            review_attempt=review_attempt,
            review_limit=self.config.document_review_limit,
        )
        self._enqueue_review_failed(run, metadata, review_attempt, mode)
        return self._create_work(
            run,
            PRODUCER_FOR_PHASE[PHASE_FOR_STAGE[metadata.stage]],
            parent_card_id,
            mode=mode,
            repair_context=context,
        )

    def _create_frozen_repair(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        phase: Phase,
        parent_card_id: str,
        violation: str,
        *,
        mode: WorkMode = WorkMode.NORMAL,
    ) -> TaskRecord:
        context = None
        if mode == WorkMode.FINALIZATION:
            latest_record = parse_card_body(history[-1].task.body)
            if (
                latest_record.repair_context is not None
                and latest_record.repair_context.kind == RepairKind.REVIEW_FAILURE
            ):
                context = latest_record.repair_context.model_copy(
                    update={
                        "issues": [
                            *latest_record.repair_context.issues,
                            "恢复冻结工件后再完成 finalization。 " + violation,
                        ]
                    }
                )
        if context is None:
            context = RepairContext(
                kind=RepairKind.FROZEN_ARTIFACT_VIOLATION,
                trigger_card_id=parent_card_id,
                issues=[
                    "恢复冻结工件到 Controller 提供的基线；只在当前阶段吸收必要适配。 "
                    + violation
                ],
            )
        self._enqueue_progress(
            run,
            f"{phase.value}:frozen-repair:{hashlib.sha256(violation.encode()).hexdigest()[:12]}",
            self._mention(run.origin)
            + f"检测到冻结工件被修改，已在 {phase.value.upper()} 阶段派发恢复任务。\n"
            f"run={run.run_key} reason={violation[:500]}",
        )
        return self._create_work(
            run,
            PRODUCER_FOR_PHASE[phase],
            parent_card_id,
            mode=mode,
            repair_context=context,
        )

    def _protocol_failures_by_stage(
        self, history: list[HistoryItem]
    ) -> dict[Stage, int]:
        result: dict[Stage, int] = {}
        for item in history:
            if item.managed.purpose != "work":
                continue
            if any(
                "[controller-protocol-error:v3]" in str(comment["body"])
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
            "mode": parse_card_body(item.task.body).mode.value,
            "project_id": run.project.project_id,
            "project_path": run.project.project_path,
            "checkout": run.workspace.checkout,
            "worktree": run.workspace.worktree,
            "branch": run.workspace.branch,
            "target_branch": run.workspace.target_branch,
            "prd_path": run.source.prd_path,
            "prd_commit_sha": run.source.prd_commit_sha,
            "prd_blob_sha": run.source.prd_blob_sha,
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
        if (
            metadata.repository_evidence is not None
            and metadata.repository_evidence.repository_base_sha
            != run.workspace.repository_base_sha
        ):
            raise ValueError(
                "repository evidence is not bound to the run base commit"
            )

    def _validate_finalization_context(
        self,
        history: list[HistoryItem],
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        if metadata.mode != WorkMode.FINALIZATION:
            return
        record = parse_card_body(item.task.body)
        context = record.repair_context
        forced = metadata.forced_advance
        if (
            context is None
            or context.kind != RepairKind.REVIEW_FAILURE
            or forced is None
        ):
            raise ValueError("finalization card is missing review failure context")
        if (
            context.review_attempt != self.config.document_review_limit
            or context.review_limit != self.config.document_review_limit
            or forced.review_limit != self.config.document_review_limit
            or forced.final_review_card_id != context.trigger_card_id
        ):
            raise ValueError("finalization review-limit evidence does not match card")
        review_item = next(
            (
                previous
                for previous in history
                if previous.task.id == context.trigger_card_id
            ),
            None,
        )
        if review_item is None:
            raise ValueError("finalization source review card was not found")
        review_metadata = validate_persisted_completion_metadata(
            review_item.task.latest_metadata
        )
        expected_review_stage = DOCUMENT_REVIEW_FOR_PRODUCER[metadata.stage]
        review_index = history.index(review_item)
        review_count = self._review_attempts_by_stage(
            history[: review_index + 1]
        ).get(expected_review_stage, 0)
        if (
            review_item.managed.stage != expected_review_stage.value
            or review_metadata.stage != expected_review_stage
            or review_metadata.outcome != Outcome.FAIL
            or review_count != self.config.document_review_limit
        ):
            raise ValueError(
                "finalization source is not the third valid failed review"
            )
        review_urls = {str(url) for url in review_metadata.gitlab_urls}
        if str(forced.final_review_url) not in review_urls:
            raise ValueError("final_review_url is not evidence from the third review")
        if str(forced.decision_url) not in {
            str(url) for url in metadata.gitlab_urls
        }:
            raise ValueError("decision_url must also appear in gitlab_urls")

    def _protocol_failure(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        latest: HistoryItem,
        reason: str,
    ) -> None:
        marker = "[controller-protocol-error:v3]"
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
            record = parse_card_body(latest.task.body)
            self._create_work(
                run,
                Stage(latest.managed.stage),
                latest.task.id,
                mode=record.mode,
                repair_context=record.repair_context,
            )
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
        outbox_key = f"{run.run_key}:exception:{suffix}"
        self.store.enqueue(
            outbox_key,
            run.run_key,
            "exception",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "自动交付需要异常处理。\n"
                f"run={run.run_key} stage=exception agent=dispatcher "
                f"card={task.id}\n"
                f"evidence={reason[:700]}\n"
                "action=请查看异常卡与证据，明确决定恢复、调整授权或停止交付。",
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
        mode: WorkMode = WorkMode.NORMAL,
        issues: list[str],
    ) -> dict:
        return CompletionMetadata(
            protocol_version="hollysys-controller/v3",
            run_key=run.run_key,
            stage=Stage(managed.stage),
            iteration=managed.iteration,
            mode=mode,
            outcome=outcome,
            project_id=run.project.project_id,
            project_path=run.project.project_path,
            checkout=run.workspace.checkout,
            worktree=run.workspace.worktree,
            branch=run.workspace.branch,
            target_branch=run.workspace.target_branch,
            prd_path=run.source.prd_path,
            prd_commit_sha=run.source.prd_commit_sha,
            prd_blob_sha=run.source.prd_blob_sha,
            prd_mr_url=run.source.prd_mr_url,
            kanban_card_id=card_id,
            issues=issues,
        ).model_dump(mode="json")

    def _latest_valid_pass(
        self, history: list[HistoryItem], stage: Stage
    ) -> CompletionMetadata | None:
        return self._latest_valid_completion(history, stage, {Outcome.PASS})

    def _latest_valid_completion(
        self,
        history: list[HistoryItem],
        stage: Stage,
        outcomes: set[Outcome],
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
                metadata = validate_persisted_completion_metadata(
                    item.task.latest_metadata
                )
                self._validate_completion_context(
                    parse_card_body(item.task.body).run,
                    item,
                    metadata,
                )
            except (ValidationError, ValueError, TypeError):
                continue
            if metadata.outcome in outcomes:
                return metadata
        return None

    def _code_modification_count(self, history: list[HistoryItem]) -> int:
        implementation_versions = 0
        for item in history:
            if (
                item.managed.purpose != "work"
                or item.managed.stage != Stage.IMPLEMENT.value
                or item.task.status != "done"
            ):
                continue
            try:
                metadata = validate_persisted_completion_metadata(
                    item.task.latest_metadata
                )
                self._validate_completion_context(
                    parse_card_body(item.task.body).run,
                    item,
                    metadata,
                )
            except (ValidationError, ValueError, TypeError):
                continue
            if metadata.outcome == Outcome.PASS:
                implementation_versions += 1
        # The first implementation is the initial version. Every later
        # successful implement completion represents one coder modification.
        return max(0, implementation_versions - 1)

    @staticmethod
    def _code_gate_issues(
        test: CompletionMetadata,
        review: CompletionMetadata,
    ) -> list[str]:
        issues = [
            *(f"[tester] {issue}" for issue in test.issues),
            *(f"[code-reviewer] {issue}" for issue in review.issues),
        ]
        # A code_gate_failure RepairContext must always have at least one
        # concrete issue. Models already require issues on a failing gate, so
        # this fallback only protects against malformed historical data.
        return issues or ["CODE 双门禁未同时通过，需人工核对门禁证据。"]

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

    def _enqueue_progress(
        self, run: RunRecord, event_key: str, text: str
    ) -> None:
        config = getattr(self, "config", None)
        if (
            config is not None
            and config.notification_level == NotificationLevel.MINIMAL
        ):
            return
        self.store.enqueue(
            f"{run.run_key}:progress:{event_key}",
            run.run_key,
            "progress",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": text,
            },
        )

    def _enqueue_phase_started(
        self, run: RunRecord, phase: Phase, task: TaskRecord
    ) -> None:
        self._enqueue_progress(
            run,
            f"{phase.value}:started",
            self._mention(run.origin)
            + f"自动交付进入 {phase.value.upper()} 阶段。\n"
            f"run={run.run_key} agent={task.assignee} card={task.id}",
        )

    def _enqueue_review_failed(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
        review_attempt: int,
        next_mode: WorkMode,
    ) -> None:
        phase = PHASE_FOR_STAGE[metadata.stage]
        summary = "；".join(metadata.issues[:3])
        if next_mode == WorkMode.FINALIZATION:
            action = "三次 review 已用尽，进入 finalization；完成关键决策后将冻结并继续。"
        else:
            action = "已退回本阶段 writer 重写，完成后再次 review。"
        self._enqueue_progress(
            run,
            f"{phase.value}:review-failed:{review_attempt}:{metadata.kanban_card_id}",
            self._mention(run.origin)
            + f"{phase.value.upper()} review 未通过 "
            f"({review_attempt}/{self.config.document_review_limit})。\n"
            f"run={run.run_key} findings={summary[:500]}\n"
            f"next_agent={self.config.stage_assignees[PRODUCER_FOR_PHASE[phase]]} "
            f"{action}",
        )

    def _enqueue_phase_frozen(
        self,
        run: RunRecord,
        phase: Phase,
        metadata: CompletionMetadata,
    ) -> None:
        disposition = metadata.baseline_disposition
        label = (
            "review 通过"
            if disposition == BaselineDisposition.REVIEWED
            else "达到 review 上限后强制收敛"
        )
        urls = " ".join(
            dict.fromkeys(
                [
                    *([str(metadata.mr_url)] if metadata.mr_url else []),
                    *(str(url) for url in metadata.gitlab_urls),
                ]
            )
        )
        risks = "；".join(metadata.residual_risk[:3]) or "无"
        decisions = "；".join(metadata.key_decisions[:3]) or "无"
        self._enqueue_progress(
            run,
            f"{phase.value}:frozen:{disposition}:{metadata.artifact_digest}",
            self._mention(run.origin)
            + f"{phase.value.upper()} 已冻结（{label}）。\n"
            f"run={run.run_key} decisions={decisions[:300]} "
            f"risks={risks[:500]}"
            + (f"\nlinks={urls}" if urls else ""),
        )

    def _enqueue_code_retry(
        self,
        run: RunRecord,
        test: CompletionMetadata,
        review: CompletionMetadata,
        next_modification: int,
    ) -> None:
        summary = "；".join(self._code_gate_issues(test, review)[:6])
        self._enqueue_progress(
            run,
            f"code:gates-failed:{review.head_sha}:modification:{next_modification}",
            self._mention(run.origin)
            + "CODE 同一提交的双门禁未同时通过，已汇总 tester 与 "
            "code-reviewer 意见退回 coder；代码 push 后将重新执行两道门禁。\n"
            f"run={run.run_key} head={review.head_sha} "
            f"tester={test.outcome.value} code-reviewer={review.outcome.value} "
            f"modification={next_modification}/"
            f"{self.config.code_modification_limit}\n"
            f"findings={summary[:700]}",
        )

    def _enqueue_test_skipped(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
    ) -> None:
        verification = "；".join(metadata.verification[:3])
        risks = "；".join(metadata.residual_risk[:3])
        self._enqueue_progress(
            run,
            f"code:test-skipped:{metadata.head_sha}:{metadata.kanban_card_id}",
            self._mention(run.origin)
            + "TEST 的必要条件经预检确认不具备，已结构化跳过该部分测试；"
            "code-reviewer 将继续审查同一提交。\n"
            f"run={run.run_key} head={metadata.head_sha} "
            f"reason={metadata.skip_reason}\n"
            f"verification={verification[:500]} risks={risks[:500]}",
        )

    def _enqueue_human_block(
        self, run: RunRecord, item: HistoryItem, comment: str
    ) -> None:
        fields = self._human_block_fields(comment)
        token = fields.get("block_id") or hashlib.sha256(
            f"{item.task.id}\0{comment}".encode()
        ).hexdigest()[:20]
        summary = fields.get("summary") or item.task.latest_summary or "工作卡暂停"
        evidence = fields.get("evidence") or "见 Kanban 脱敏阻塞评论"
        action = fields.get("required_action") or "按阻塞评论完成一个明确动作"
        self.store.enqueue(
            f"{run.run_key}:human-block:{token}",
            run.run_key,
            "human-block",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "自动交付遇到真正阻塞，需要你的处理。\n"
                f"run={run.run_key} stage={item.managed.stage} "
                f"agent={item.task.assignee} card={item.task.id}\n"
                f"summary={summary[:300]}\nevidence={evidence[:300]}\n"
                f"action={action[:300]}",
            },
        )

    @staticmethod
    def _human_block_fields(comment: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for raw_line in comment.splitlines():
            key, separator, value = raw_line.partition(":")
            normalized = key.strip()
            if separator and normalized in PARSED_HUMAN_BLOCK_FIELDS:
                fields[normalized] = value.strip()
        return fields

    @staticmethod
    def _valid_human_block(fields: dict[str, str]) -> bool:
        if (
            REQUIRED_HUMAN_BLOCK_FIELDS - fields.keys()
            or fields.get("kind") not in ALLOWED_HUMAN_BLOCK_KINDS
        ):
            return False
        if fields.get("kind") in {"environment", "destructive_approval"}:
            if GATED_HUMAN_BLOCK_FIELDS - fields.keys():
                return False
            if fields.get("gate_phase") not in {
                "migration",
                "deployment",
                "release",
            }:
                return False
            if not fields.get("requirement_ids") or not fields.get(
                "contract_refs"
            ):
                return False
        return True

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
        managed = self.store.managed_card(run.workspace.board, event.task_id)
        task = self.reader.task(run.workspace.board, event.task_id)
        self.store.enqueue(
            key,
            run.run_key,
            "failure-limit",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "Hermes 工作卡已触发失败熔断，需要人工检查。\n"
                f"run={run.run_key} stage={managed.stage if managed else 'unknown'} "
                f"agent={task.assignee if task else 'unknown'} card={event.task_id} "
                f"event={event.kind}\nevidence={details}\n"
                "action=检查该卡的脱敏运行证据并修复环境后查询 status",
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
                f"run={run_key}\nevidence={reason[:700]}\n"
                "action=修复 Controller/GitLab/Kanban 连通性后运行 health 和 status",
            },
        )

    def _enqueue_dependency_outage(self, run_key: str, outage: dict) -> None:
        try:
            _, run = self._history(run_key)
        except Exception:  # noqa: BLE001 - best-effort operator notice
            return
        outage_id = str(outage["outage_id"])
        self.store.enqueue(
            f"{run_key}:dependency-outage:{outage_id}",
            run_key,
            "dependency-outage",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "外部依赖暂时不可用，当前 Run 已进入自动退避。\n"
                f"run={run_key} dependency=gitlab outage={outage_id} "
                f"class={outage['error_class']} failures={outage['failures']} "
                f"next_retry_at={outage['next_retry_at']}\n"
                "Controller、状态查询及其他本地服务保持运行，无需重启容器。",
            },
        )

    def _enqueue_dependency_recovered(
        self,
        run_key: str,
        outage: dict,
    ) -> None:
        try:
            _, run = self._history(run_key)
        except Exception:  # noqa: BLE001 - best-effort operator notice
            return
        outage_id = str(outage["outage_id"])
        duration = max(
            0,
            int(time.time()) - int(outage["started_at"]),
        )
        self.store.enqueue(
            f"{run_key}:dependency-recovered:{outage_id}",
            run_key,
            "dependency-recovered",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "外部依赖已恢复，Controller 正按原 run/worktree/MR 继续对账。\n"
                f"run={run_key} dependency=gitlab outage={outage_id} "
                f"duration_seconds={duration} retries={outage['failures']}",
            },
        )

    @staticmethod
    def _dependency_error_class(error: Exception) -> str:
        text = f"{type(error).__name__}: {error}".lower()
        if any(
            token in text
            for token in ("401", "403", "unauthorized", "forbidden")
        ):
            return "dependency_auth"
        if any(token in text for token in ("429", "rate limit", "retry-after")):
            return "dependency_rate_limited"
        if any(token in text for token in ("400", "404", "422")):
            return "dependency_contract"
        return "dependency_transient"

    @staticmethod
    def _merge_blocker_kind(error: Exception) -> str:
        text = str(error).lower()
        if "draft" in text or "work in progress" in text:
            return "draft"
        if "pipeline" in text and any(
            token in text for token in ("failed", "skipped", "canceled")
        ):
            return "pipeline_failed"
        if "pipeline" in text:
            return "pipeline_pending"
        if "discussion" in text:
            return "unresolved_discussion"
        if "merge" in text:
            return "not_mergeable"
        return "external_gate"

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
                default=str,
            )
        return str(error)
