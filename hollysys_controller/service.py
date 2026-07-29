from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .config import ControllerConfig
from .errors import (
    ControllerFatalError,
    DependencyAuthError,
    DependencyContractError,
    DependencyError,
    DependencyRateLimitedError,
    DependencyTransientError,
    ErrorContext,
    MergeBlocked,
    ReconcileSuperseded,
    RunPolicyError,
)
from .git_auth import summarize_profile_preflight
from .gitlab import (
    CONTROLLER_MERGE_SUBMITTED_FIELD,
    CheckedHeadConflict,
    GitLabClient,
)
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
    RecoverRequest,
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
from .store import TERMINAL_RUN_STATES, ControllerStore, ManagedCard
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
        # Kept for compatibility with integrations that inspect this member.
        # No network operation is performed while holding it.
        self._lock = threading.RLock()
        self._inflight_guard = threading.Lock()
        self._reconciling_runs: set[str] = set()
        self._aborting_runs: set[str] = set()
        self._active_requests: set[str] = set()
        self._audit_lock = threading.Lock()
        self._outbox_lock = threading.Lock()
        self.last_poll_at: int | None = None
        self.last_poll_error: str | None = None
        self.last_reconcile_at: int | None = None
        self.last_reconcile_error: str | None = None

    def start(self, raw: dict) -> dict:
        if getattr(self.config, "controller_mode", "active") != "active":
            raise ValueError("controller_preflight_mode")
        request = StartRequest.model_validate(raw)
        request_key = f"start:{request.message_id}"
        if not self._begin_request_execution(request_key):
            raise RunPolicyError(f"request_in_progress:{request_key}")
        try:
            return self._start(raw)
        finally:
            self._finish_request_execution(request_key)

    def _begin_reconcile(self, run_key: str) -> bool:
        if not hasattr(self, "_inflight_guard"):
            self._inflight_guard = threading.Lock()
        if not hasattr(self, "_reconciling_runs"):
            self._reconciling_runs = set()
        with self._inflight_guard:
            if run_key in self._reconciling_runs:
                return False
            self._reconciling_runs.add(run_key)
            return True

    def _finish_reconcile(self, run_key: str) -> None:
        with self._inflight_guard:
            self._reconciling_runs.discard(run_key)

    def _begin_abort(self, run_key: str) -> bool:
        if not hasattr(self, "_inflight_guard"):
            self._inflight_guard = threading.Lock()
        if not hasattr(self, "_aborting_runs"):
            self._aborting_runs = set()
        with self._inflight_guard:
            if run_key in self._aborting_runs:
                return False
            self._aborting_runs.add(run_key)
            return True

    def _finish_abort(self, run_key: str) -> None:
        with self._inflight_guard:
            self._aborting_runs.discard(run_key)

    def _begin_request_execution(self, request_key: str) -> bool:
        if not hasattr(self, "_inflight_guard"):
            self._inflight_guard = threading.Lock()
        if not hasattr(self, "_active_requests"):
            self._active_requests = set()
        with self._inflight_guard:
            if request_key in self._active_requests:
                return False
            self._active_requests.add(request_key)
            return True

    def _finish_request_execution(self, request_key: str) -> None:
        with self._inflight_guard:
            self._active_requests.discard(request_key)

    def _assert_reconcile_mutable(self, run_key: str) -> dict:
        control = self._run_control(run_key)
        state = str(control.get("state") or "active")
        if state not in {"active", "dependency_degraded", "merge_wait"}:
            raise ReconcileSuperseded(
                f"reconcile_superseded:{run_key}:state={state}"
            )
        return control

    def _start(self, raw: dict) -> dict:
        request = StartRequest.model_validate(raw)
        key = f"start:{request.message_id}"
        dependency_run_key: str | None = None
        run_claimed = False
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
            dependency_run_key = run.run_key
            self.store.ensure_run_control(run.run_key)
            self.store.bind_request_run(key, run.run_key)
            if not self._begin_reconcile(run.run_key):
                raise RunPolicyError(f"request_in_progress:{key}")
            run_claimed = True
            existing = self.store.cards_for_run(run.run_key)
            if existing:
                response = self.status_summary(run.run_key)
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
        except DependencyContractError as exc:
            self.store.fail_request(key, str(exc))
            raise
        except DependencyError as exc:
            self._record_request_dependency_error(dependency_run_key, exc)
            raise
        except RunPolicyError as exc:
            if not str(exc).startswith("request_in_progress:"):
                self.store.fail_request(key, str(exc))
            raise
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise
        finally:
            if run_claimed and dependency_run_key is not None:
                self._finish_reconcile(dependency_run_key)

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
            control = self._run_control(run_key)
            control_state = str(control.get("state") or "active")
            exact_stage = (
                current.managed.stage
                if current
                else "merged"
                if merged
                else "checked-head-merge"
                if latest_work
                and latest_work.managed.stage == Stage.CODE_REVIEW.value
                and latest_work.task.status == "done"
                else control_state
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
                else "terminal"
                if control_state in TERMINAL_RUN_STATES
                else control_state
            )
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
        control = self._run_control(run_key)
        control_state = str(control.get("state") or "active")
        exact_stage = (
            current.managed.stage
            if current
            else "merge-wait"
            if merge_wait
            else control_state
        )
        phase = (
            PHASE_FOR_STAGE[Stage(current.managed.stage)].value
            if current and current.managed.purpose == "work"
            else "exception"
            if current
            else Phase.CODE.value
            if merge_wait
            else "terminal"
            if control_state in TERMINAL_RUN_STATES
            else control_state
        )
        code_modifications = self._code_modification_count(history)
        store_health = self.store.health()
        controller_cursor = int(
            store_health["event_cursors"].get(run.workspace.board, 0)
        )
        kanban_max_event_id = self.reader.max_event_id(run.workspace.board)

        return {
            "run_key": run_key,
            "control": control,
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

    def preflight(self, *, deep: bool = False) -> dict:
        if self.config.controller_mode != "preflight":
            raise ValueError("preflight_requires_controller_preflight_mode")
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
        agent_git = Path(self.config.agent_git_command)
        askpass = agent_git.with_name("gitlab-askpass")
        credential_helper = agent_git.with_name("gitlab-credential")
        checks["agent_git_wrapper"] = {
            "ok": self._protected_executable(agent_git),
            "path": str(agent_git),
        }
        checks["agent_git_askpass"] = {
            "ok": self._protected_executable(askpass),
            "path": str(askpass),
        }
        checks["agent_git_credential_helper"] = {
            "ok": self._protected_executable(credential_helper),
            "path": str(credential_helper),
        }
        checks["agent_glab"] = {
            "ok": self._protected_executable(agent_git.with_name("glab")),
            "path": str(agent_git.with_name("glab")),
        }
        checks["agent_lark_cli"] = {
            "ok": self._protected_executable(agent_git.with_name("lark-cli")),
            "path": str(agent_git.with_name("lark-cli")),
        }
        profile_credentials = summarize_profile_preflight(
            self.config,
            deep=deep,
        )
        credential_contract_digest = str(
            profile_credentials.pop("_credential_contract_digest")
        )
        checks["profile_credentials"] = profile_credentials
        for profile_result in profile_credentials["profiles"]:
            self.store.record_profile_preflight(profile_result, deep=deep)
        result = {
            "ok": all(item["ok"] for item in checks.values()),
            "mode": "deep" if deep else "static",
            "checks": checks,
        }
        self.store.record_deployment_preflight(
            ok=bool(result["ok"]),
            deep=deep,
            credential_contract_digest=credential_contract_digest,
        )
        return result

    def assert_activation_preflight(self) -> None:
        """Fail closed if active mode did not pass the current deep contract."""
        accepted = self.store.deployment_preflight(include_digest=True)
        if not accepted or not accepted["ok"] or not accepted["deep"]:
            raise ControllerFatalError(
                "active_mode_requires_successful_deep_preflight"
            )
        current = summarize_profile_preflight(self.config, deep=False)
        current_digest = str(current.pop("_credential_contract_digest"))
        if (
            not current["ok"]
            or current_digest != accepted["credential_contract_digest"]
        ):
            raise ControllerFatalError(
                "profile_credentials_changed_after_deep_preflight"
            )
        for path in (
            Path(self.config.agent_git_command),
            Path(self.config.agent_git_command).with_name("gitlab-askpass"),
            Path(self.config.agent_git_command).with_name("gitlab-credential"),
            Path(self.config.agent_git_command).with_name("glab"),
            Path(self.config.agent_git_command).with_name("lark-cli"),
        ):
            if not self._protected_executable(path):
                raise ControllerFatalError(
                    f"agent_git_auth_not_protected:{path.name}"
                )

    @staticmethod
    def _protected_executable(path: Path) -> bool:
        if (
            path.is_symlink()
            or not path.is_file()
            or not os.access(path, os.X_OK)
        ):
            return False
        info = path.stat()
        return info.st_uid == 0 and info.st_mode & 0o022 == 0

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
        self._validate_semantic_gate(run, item, metadata)
        if metadata.repository_evidence is not None:
            self.gitlab.validate_repository_evidence(run, metadata)
            self.gitlab.validate_author_completion(run, metadata)
        if metadata.stage in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
            Stage.TEST,
            Stage.CODE_REVIEW,
        }:
            gate_author = self.gitlab.validate_gate(run, metadata)
            self._validate_gate_reviewer(metadata, gate_author)
        return {
            "ok": True,
            "run_key": run.run_key,
            "card_id": item.task.id,
            "stage": metadata.stage.value,
            "iteration": metadata.iteration,
            "context": "controller-verified",
        }

    def abort_request(self, raw: dict) -> dict:
        if getattr(self.config, "controller_mode", "active") != "active":
            raise ValueError("controller_preflight_mode")
        request = AbortRequest.model_validate(raw)
        key = f"abort-request:{request.message_id}"
        if not self._begin_request_execution(key):
            raise RunPolicyError(f"request_in_progress:{key}")
        try:
            previous = self.store.begin_request(
                key, "abort-request", request.model_dump(mode="json")
            )
        except Exception:
            self._finish_request_execution(key)
            raise
        if previous is not None:
            self._finish_request_execution(key)
            return {
                **previous,
                "error_code": "token_unavailable",
                "reissue_required": True,
            }
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
            if (
                request.chat_id != run.origin.chat_id
                or (request.thread_id or None) != (run.origin.thread_id or None)
            ):
                raise PermissionError(
                    "abort must be requested in the original run chat/thread"
                )
            control = self.store.run_control(request.run_key)
            if control and control["state"] in TERMINAL_RUN_STATES | {
                "abort_requested",
                "aborting",
            }:
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
        except DependencyContractError as exc:
            self.store.fail_request(key, str(exc))
            raise
        except DependencyError as exc:
            self._record_request_dependency_error(request.run_key, exc)
            raise
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise
        finally:
            self._finish_request_execution(key)

    def abort_confirm(self, raw: dict) -> dict:
        if getattr(self.config, "controller_mode", "active") != "active":
            raise ValueError("controller_preflight_mode")
        request = AbortConfirmRequest.model_validate(raw)
        key = f"abort-confirm:{request.message_id}"
        request_payload = request.model_dump(
            mode="json",
            exclude={"token"},
        )
        request_payload["token_hash"] = hashlib.sha256(
            request.token.encode("ascii")
        ).hexdigest()
        if not self._begin_request_execution(key):
            raise RunPolicyError(f"request_in_progress:{key}")
        try:
            previous = self.store.begin_request(
                key, "abort-confirm", request_payload
            )
        except Exception:
            self._finish_request_execution(key)
            raise
        if previous is not None:
            self._finish_request_execution(key)
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
            response = {
                "run_key": request.run_key,
                "state": control["state"],
                "reason": control.get("abort_reason"),
                "continuation": "pending-reconcile",
            }
            self.store.finish_request(key, response)
            return response
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise
        finally:
            self._finish_request_execution(key)

    def resolve(self, raw: dict) -> dict:
        if getattr(self, "config", None) is not None and getattr(
            self.config,
            "controller_mode",
            "active",
        ) != "active":
            raise ValueError("controller_preflight_mode")
        request = ResolveRequest.model_validate(raw)
        key = f"resolve:{request.block_id}:{request.message_id}"
        run_claimed = False
        if not self._begin_request_execution(key):
            raise RunPolicyError(f"request_in_progress:{key}")
        try:
            previous = self.store.begin_request(
                key, "resolve", request.model_dump(mode="json")
            )
        except Exception:
            self._finish_request_execution(key)
            raise
        if previous is not None:
            self._finish_request_execution(key)
            return previous
        try:
            if not self._begin_reconcile(request.run_key):
                raise RunPolicyError(f"request_in_progress:{key}")
            run_claimed = True
            # Keep the existing scoped block for readability; the in-flight
            # claim above, not a held lock, serializes this run mutation.
            with nullcontext():
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
                if not self._valid_human_block(
                    block_fields,
                    stage=Stage(managed.stage),
                ):
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
        except DependencyContractError as exc:
            self.store.fail_request(key, str(exc))
            raise
        except DependencyError as exc:
            self._record_request_dependency_error(request.run_key, exc)
            raise
        except RunPolicyError as exc:
            if not str(exc).startswith("request_in_progress:"):
                self.store.fail_request(key, str(exc))
            raise
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise
        finally:
            if run_claimed:
                self._finish_reconcile(request.run_key)
            self._finish_request_execution(key)

    def recover(self, raw: dict) -> dict:
        if getattr(self.config, "controller_mode", "active") != "active":
            raise ValueError("controller_preflight_mode")
        request = RecoverRequest.model_validate(raw)
        key = f"recover:{request.message_id}"
        run_claimed = False
        if not self._begin_request_execution(key):
            raise RunPolicyError(f"request_in_progress:{key}")
        try:
            previous = self.store.begin_request(
                key,
                "recover",
                request.model_dump(mode="json"),
            )
        except Exception:
            self._finish_request_execution(key)
            raise
        if previous is not None:
            self._finish_request_execution(key)
            return previous
        try:
            if not self._begin_reconcile(request.run_key):
                raise RunPolicyError(f"request_in_progress:{key}")
            run_claimed = True
            history, run = self._history(request.run_key)
            if (
                request.sender != run.origin.initiator_open_id
                and request.sender not in self.config.abort_admin_open_ids
            ):
                raise PermissionError(
                    "only the run initiator or configured administrator "
                    "may recover this run"
                )
            if (
                request.chat_id != run.origin.chat_id
                or (request.thread_id or None) != (run.origin.thread_id or None)
            ):
                raise PermissionError(
                    "recovery must be requested in the original run chat/thread"
                )
            control = self.store.run_control(request.run_key)
            if control is None or control["state"] != "exception":
                raise ValueError("run is not in recoverable exception state")
            for item in history:
                if (
                    item.managed.purpose == "exception"
                    and item.task.status in ACTIVE_STATUSES
                ):
                    self.kanban.abort_task(
                        run.workspace.board,
                        item.task.id,
                        "exception recovery authorized: " + request.reason[:500],
                    )
            recovered = self.store.transition_run(
                request.run_key,
                expected_states={"exception"},
                new_state="active",
                reason=f"human_exception_recovery:{request.reason}",
                expected_version=int(control["state_version"]),
            )
            self.store.enqueue(
                (
                    f"{request.run_key}:exception-recovered:"
                    f"{recovered['state_version']}"
                ),
                request.run_key,
                "exception-recovered",
                {
                    "origin": run.origin.model_dump(mode="json"),
                    "text": self._mention(run.origin)
                    + "人类已授权从异常状态恢复自动交付。\n"
                    f"run={request.run_key} sender={request.sender} "
                    f"reason={request.reason[:500]}",
                },
            )
            response = {
                "run_key": request.run_key,
                "state": recovered["state"],
                "state_version": recovered["state_version"],
                "continuation": "pending-reconcile",
            }
            self.store.finish_request(key, response)
            return response
        except DependencyContractError as exc:
            self.store.fail_request(key, str(exc))
            raise
        except DependencyError as exc:
            self._record_request_dependency_error(request.run_key, exc)
            raise
        except RunPolicyError as exc:
            if not str(exc).startswith("request_in_progress:"):
                self.store.fail_request(key, str(exc))
            raise
        except Exception as exc:
            self.store.fail_request(key, str(exc))
            raise
        finally:
            if run_claimed:
                self._finish_reconcile(request.run_key)
            self._finish_request_execution(key)

    def poll_once(self) -> None:
        try:
            pending_reconcile: set[str] = set()
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
                    # The lifecycle observation is durable once recorded.
                    # Advance the cursor before network reconciliation so a
                    # GitLab outage cannot replay one terminal event forever.
                    self.store.set_cursor(board, event.id)
                    if not managed or event.kind not in TERMINAL_EVENT_KINDS:
                        continue
                    try:
                        if event.kind in {"gave_up", "spawn_auto_blocked"}:
                            _, run = self._history(managed.run_key)
                            self._enqueue_failure_limit(run, event)
                        pending_reconcile.add(managed.run_key)
                    except ReconcileSuperseded:
                        continue
                    except DependencyContractError as exc:
                        self._record_run_exception(managed.run_key, exc)
                    except DependencyError as exc:
                        self._handle_dependency_error(managed.run_key, exc)
                    except RunPolicyError as exc:
                        self._record_run_exception(managed.run_key, exc)
                    except (ValidationError, ValueError, TypeError) as exc:
                        self._record_run_exception(managed.run_key, exc)
                    except ControllerFatalError:
                        raise
                    except Exception as exc:
                        raise ControllerFatalError(
                            f"poll_reconcile_failed:{managed.run_key}:{exc}"
                        ) from exc
            self._reconcile_run_keys(sorted(pending_reconcile))
            self.flush_outbox()
            self.last_poll_at = int(time.time())
            self.last_poll_error = None
        except Exception as exc:
            self.last_poll_error = self._error_text(exc)
            raise

    def reconcile_all(self) -> None:
        try:
            if self.config.controller_mode != "active":
                self.last_reconcile_at = int(time.time())
                self.last_reconcile_error = None
                self.flush_outbox()
                return
            for request in self.store.running_requests():
                try:
                    kind = str(request["kind"])
                    if kind in {
                        "abort-request",
                        "abort-confirm",
                    }:
                        self._recover_sensitive_request(request)
                        continue
                    dependencies = {
                        "start": {"gitlab", "kanban"},
                        "resolve": {"gitlab", "kanban"},
                        "recover": {"kanban"},
                    }.get(kind, set())
                    request_run_key = str(
                        request.get("run_key")
                        or request["payload"].get("run_key")
                        or ""
                    )
                    dependency_scopes = {
                        scope
                        for dependency in dependencies
                        for scope in (
                            dependency,
                            (
                                f"{dependency}:{request_run_key}"
                                if request_run_key
                                else f"{dependency}:request"
                            ),
                        )
                    }
                    if (
                        "gitlab" in dependencies
                        and self._dependency_is_open("gitlab")
                    ):
                        continue
                    if any(
                        self._dependency_retry_blocked(scope)
                        for scope in dependency_scopes
                    ):
                        continue
                    response: dict | None = None
                    if kind == "start":
                        response = self.start(request["payload"])
                    elif kind == "resolve":
                        response = self.resolve(request["payload"])
                    elif kind == "recover":
                        response = self.recover(request["payload"])
                    if response and "kanban" in dependencies:
                        run_key = str(response.get("run_key") or "")
                        if run_key:
                            self._recover_run_dependency(run_key, "kanban")
                    if response and "gitlab" in dependencies:
                        run_key = str(response.get("run_key") or "")
                        if run_key:
                            self._recover_run_dependency(run_key, "gitlab")
                    for dependency in dependencies:
                        self._recover_request_dependency(dependency)
                except DependencyError:
                    # The durable request stays running and is retried after
                    # the dependency circuit allows it.
                    continue
                except ControllerFatalError:
                    raise
                except RunPolicyError as exc:
                    if str(exc).startswith("request_in_progress:"):
                        continue
                    LOG.warning(
                        "persisted request %s could not be resumed: %s",
                        request["request_key"],
                        self._error_text(exc),
                    )
                except Exception as exc:  # noqa: BLE001 - request recovery boundary
                    LOG.warning(
                        "persisted request %s finished with a request-level "
                        "error during recovery: %s",
                        request["request_key"],
                        self._error_text(exc),
                    )
            for run_key in self.store.active_abort_run_keys():
                try:
                    if self._continue_abort(run_key):
                        self._recover_run_dependency(run_key, "kanban")
                        self._recover_run_dependency(run_key, "gitlab")
                except DependencyContractError as exc:
                    self._enqueue_controller_failure(run_key, exc)
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                except ControllerFatalError:
                    raise
                except Exception as exc:
                    raise ControllerFatalError(
                        f"abort_reconcile_failed:{run_key}:{exc}"
                    ) from exc
            if self._dependency_retry_blocked("gitlab"):
                self.last_reconcile_at = int(time.time())
                self.last_reconcile_error = "gitlab circuit is in backoff"
                self.flush_outbox()
                return
            self._recover_gitlab_circuit()
            if self._dependency_retry_blocked("gitlab"):
                self.last_reconcile_at = int(time.time())
                self.flush_outbox()
                return
            self._enqueue_stale_worker_notices()
            self._reconcile_run_keys(self.store.active_reconcile_run_keys())
            self.last_reconcile_at = int(time.time())
            self.last_reconcile_error = None
            self.flush_outbox()
        except Exception as exc:
            self.last_reconcile_error = str(exc)
            raise

    def _recover_sensitive_request(self, request: dict) -> None:
        """Close a crash-interrupted abort RPC without replaying a secret.

        Abort request tokens are intentionally never persisted in plaintext.
        A confirmation may, however, have atomically changed run_control before
        the RPC response was saved. Preserve that committed state and let the
        normal abort reconciler continue it.
        """

        key = str(request["request_key"])
        kind = str(request["kind"])
        payload = request["payload"]
        run_key = str(payload.get("run_key") or "")
        if kind == "abort-confirm":
            control = self.store.run_control(run_key)
            if control and control["state"] in {
                "abort_requested",
                "aborting",
                *TERMINAL_RUN_STATES,
            }:
                self.store.finish_request(
                    key,
                    {
                        "run_key": run_key,
                        "state": control["state"],
                        "reason": control.get("abort_reason"),
                        "continuation": (
                            "complete"
                            if control["state"] in TERMINAL_RUN_STATES
                            else "pending-reconcile"
                        ),
                    },
                )
                return
        self.store.finish_request(
            key,
            {
                "run_key": run_key,
                "error_code": "token_unavailable",
                "reissue_required": True,
                "message": (
                    "the controller restarted before the secret-bearing abort "
                    "request was durably completed; send a new Feishu message "
                    "to request a new confirmation token"
                ),
            },
        )

    def reconcile_run(self, run_key: str) -> bool:
        if not self._begin_reconcile(run_key):
            return False
        try:
            self._reconcile_run(run_key)
            return True
        except ReconcileSuperseded:
            LOG.info("discarded stale reconcile result for run %s", run_key)
            return False
        finally:
            self._finish_reconcile(run_key)

    def _reconcile_run_with_policy(self, run_key: str) -> None:
        if self._dependency_retry_blocked("gitlab"):
            return
        try:
            if self.reconcile_run(run_key):
                self._recover_run_dependency(run_key, "kanban")
                self._recover_run_dependency(run_key, "gitlab")
        except DependencyContractError as exc:
            self._record_run_exception(run_key, exc)
        except DependencyError as exc:
            self._handle_dependency_error(run_key, exc)
        except RunPolicyError as exc:
            self._record_run_exception(run_key, exc)
        except (ValidationError, ValueError, TypeError) as exc:
            self._record_run_exception(run_key, exc)
        except ControllerFatalError:
            raise
        except Exception as exc:
            raise ControllerFatalError(
                f"run_reconcile_failed:{run_key}:{exc}"
            ) from exc

    def _reconcile_run_keys(self, run_keys: list[str]) -> None:
        unique = list(dict.fromkeys(run_keys))
        if not unique:
            return
        workers = min(
            len(unique),
            int(getattr(self.config, "reconcile_workers", 4)),
        )
        if workers == 1:
            self._reconcile_run_with_policy(unique[0])
            return
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="hollysys-reconcile",
        ) as executor:
            futures = {
                executor.submit(self._reconcile_run_with_policy, run_key): run_key
                for run_key in unique
            }
            for future in as_completed(futures):
                future.result()

    def _reconcile_run(self, run_key: str) -> None:
        control = self._run_control(run_key)
        if control and control["state"] in {
            "abort_requested",
            "aborting",
        }:
            self._continue_abort(run_key)
            return
        if control and control["state"] in TERMINAL_RUN_STATES | {"exception"}:
            return
        if self._dependency_retry_blocked("gitlab"):
            return
        history, run = self._history(run_key)
        mr = self.gitlab.delivery_mr(run)
        if mr and mr.get("state") == "merged":
            self._finalize_merged(run, history, mr)
            return
        self._assert_reconcile_mutable(run_key)
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
            self._assert_reconcile_mutable(run_key)
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
                if failure_fuse:
                    failure_kind = next(
                        kind
                        for kind in reversed(item.task.event_kinds)
                        if kind in {"gave_up", "spawn_auto_blocked"}
                    )
                    self._assert_reconcile_mutable(run_key)
                    self.kanban.abort_task(
                        run.workspace.board,
                        item.task.id,
                        "worker redispatch budget exhausted: "
                        f"{failure_kind}",
                    )
                    self._exception(
                        run,
                        item.task.id,
                        "worker redispatch budget exhausted; "
                        f"stage={item.managed.stage}; "
                        f"card={item.task.id}; "
                        f"event={failure_kind}; "
                        f"limit={self.config.worker_redispatch_limit}",
                    )
                    return
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
                    self._assert_reconcile_mutable(run_key)
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
                    if not self._valid_human_block(
                        block_fields,
                        stage=Stage(item.managed.stage),
                    ):
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
                            self._assert_reconcile_mutable(run_key)
                            self.kanban.comment(
                                run.workspace.board,
                                item.task.id,
                                "[controller-block-rejected:v3]\n"
                                f"reason: {reason}",
                                "hollysys-controller",
                            )
                        self._assert_reconcile_mutable(run_key)
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
            self._validate_worker_attempt(latest)
            metadata = validate_persisted_completion_metadata(
                latest.task.latest_metadata
            )
            self._validate_completion_context(run, latest, metadata)
            self._validate_finalization_context(history, latest, metadata)
            self._validate_semantic_gate(run, latest, metadata)
        except (ValidationError, ValueError, TypeError) as exc:
            self._protocol_failure(run, history, latest, self._error_text(exc))
            return
        if metadata.repository_evidence is not None:
            try:
                self.gitlab.validate_repository_evidence(run, metadata)
                self.gitlab.validate_author_completion(run, metadata)
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
                gate_author = self.gitlab.validate_gate(run, metadata)
                self._validate_gate_reviewer(metadata, gate_author)
            except ValueError as exc:
                # A push invalidates test/code-review and deterministically
                # restarts at test; other gate mismatches are protocol retries.
                if metadata.stage in {Stage.TEST, Stage.CODE_REVIEW} and (
                    "current MR head" in str(exc) or "not bound" in str(exc)
                ):
                    self._reject_agent_completion(
                        run,
                        latest,
                        str(exc),
                        mr_iid=metadata.mr_iid,
                        head_sha=metadata.head_sha,
                    )
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
                self._reject_agent_completion(
                    run,
                    latest,
                    violation,
                    mr_iid=metadata.mr_iid,
                    head_sha=current_head,
                )
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
                    self._reject_agent_completion(
                        run,
                        latest,
                        str(exc),
                        mr_iid=metadata.mr_iid,
                        head_sha=current_head,
                    )
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
                    self._reject_agent_completion(
                        run,
                        latest,
                        str(exc),
                        mr_iid=metadata.mr_iid,
                        head_sha=current_head,
                    )
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

        self._assert_reconcile_mutable(run.run_key)
        self._record_attempt_completion(
            latest,
            board=latest.managed.board,
            accepted=True,
            mr_iid=metadata.mr_iid,
            head_sha=metadata.head_sha,
        )
        self._enqueue_agent_completed(run, latest, metadata)

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
                gate_author = self.gitlab.validate_gate(run, paired_test)
                self._validate_gate_reviewer(paired_test, gate_author)
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
            except MergeBlocked as exc:
                self._handle_merge_blocker(
                    run,
                    parent_card_id=latest.task.id,
                    live_mr=live_mr,
                    mr_iid=int(test.mr_iid),
                    current_head=current_head,
                    blocker=exc,
                )
                return
            except ValueError as exc:
                if (
                    "current MR head" in str(exc)
                    or "not valid for current MR head" in str(exc)
                    or "refer to another MR" in str(exc)
                ):
                    self.store.clear_merge_wait(run.run_key)
                    self._create_work(run, Stage.TEST, latest.task.id)
                else:
                    raise RunPolicyError(
                        f"merge_validation_contract:{exc}"
                    ) from exc
                return
            if mr.get("state") == "merged":
                self._finalize_merged(run, history, mr)
                return
            self.store.clear_merge_wait(run.run_key)
            current_control = self._run_control(run.run_key)
            if current_control["state"] == "merge_wait":
                self.store.transition_run(
                    run.run_key,
                    expected_states={"merge_wait"},
                    new_state="active",
                    reason="merge_blockers_cleared",
                    expected_version=int(current_control["state_version"]),
                    checked_head=checked_head,
                )
            operation_key = f"{run.run_key}:merge:{checked_head}"
            operation_control = self._run_control(run.run_key)
            try:
                merged = self._operation(
                    operation_key,
                    "checked-head-merge",
                    {
                        "project_id": run.project.project_id,
                        "mr_iid": int(mr["iid"]),
                        "checked_head": checked_head,
                    },
                    lambda: self._merge_after_revalidation(
                        run,
                        int(mr["iid"]),
                        test,
                        review,
                        checked_head,
                    ),
                    run_key=run.run_key,
                    expected_state_version=int(
                        operation_control.get("state_version") or 1
                    ),
                    expected_head_sha=checked_head,
                )
            except MergeBlocked as exc:
                self._handle_merge_blocker(
                    run,
                    parent_card_id=latest.task.id,
                    live_mr=mr,
                    mr_iid=int(test.mr_iid),
                    current_head=checked_head,
                    blocker=exc,
                )
                return
            except CheckedHeadConflict:
                self._create_work(run, Stage.TEST, latest.task.id)
                return
            self._finalize_merged(
                run,
                history,
                merged,
                operation_key=operation_key,
            )

    def _handle_merge_blocker(
        self,
        run: RunRecord,
        *,
        parent_card_id: str,
        live_mr: dict,
        mr_iid: int,
        current_head: str,
        blocker: MergeBlocked,
    ) -> None:
        waiting = self.store.set_merge_wait(
            run.run_key,
            mr_iid=mr_iid,
            head_sha=current_head,
            blocker_kind=blocker.kind,
            blocker=self._error_text(blocker),
            blocker_url=blocker.url,
            blocker_owner=blocker.owner,
            blocker_updated_at=blocker.updated_at,
            retry_seconds=self.config.merge_wait_retry_seconds,
        )
        current_control = self._run_control(run.run_key)
        merge_wait_control = current_control
        if current_control["state"] in {
            "active",
            "dependency_degraded",
            "merge_wait",
        }:
            merge_wait_control = self.store.transition_run(
                run.run_key,
                expected_states={str(current_control["state"])},
                new_state="merge_wait",
                reason=f"merge_blocked:{blocker.kind}",
                expected_version=int(
                    current_control.get("state_version") or 1
                ),
                next_retry_at=int(waiting["next_retry_at"]),
                checked_head=current_head,
            )
        if waiting["changed"] or blocker.immediate_exception:
            self._enqueue_progress(
                run,
                (
                    f"merge-wait:{blocker.kind}:{current_head}:"
                    f"{merge_wait_control['state_version']}"
                ),
                self._mention(run.origin)
                + "代码门禁已完成，但合并条件尚未满足。\n"
                f"run={run.run_key} stage=merge-wait "
                f"blocker={blocker.kind} mr={live_mr.get('web_url')} "
                f"head={current_head} owner={blocker.owner or 'unknown'} "
                f"url={blocker.url or live_mr.get('web_url')} "
                f"next_retry_at={waiting['next_retry_at']}",
                allow_minimal=blocker.kind
                in {
                    "draft",
                    "approval_missing",
                    "discussion_unresolved",
                    "not_mergeable",
                },
            )
        timeout = (
            self.config.merge_draft_grace_seconds
            if blocker.kind == "draft"
            else self.config.merge_blocker_timeout_seconds
        )
        elapsed = int(time.time()) - int(waiting["first_seen_at"])
        if blocker.immediate_exception or elapsed >= timeout:
            reason = (
                f"merge blocker {blocker.kind} "
                f"{'requires immediate action' if blocker.immediate_exception else 'timed out'}; "
                f"head={current_head}; "
                f"url={blocker.url or live_mr.get('web_url')}; "
                f"owner={blocker.owner or 'unknown'}; evidence={blocker}"
            )
            self._exception(run, parent_card_id, reason)

    def _merge_after_revalidation(
        self,
        run: RunRecord,
        mr_iid: int,
        test: CompletionMetadata,
        review: CompletionMetadata,
        checked_head: str,
    ) -> dict:
        try:
            current, current_head = self.gitlab.validate_merge(
                run,
                mr_iid=mr_iid,
                test=test,
                code_review=review,
            )
        except ValueError as exc:
            raise CheckedHeadConflict(
                "merge evidence changed before checked-head merge"
            ) from exc
        if current.get("state") == "merged":
            return current
        if current_head != checked_head:
            raise CheckedHeadConflict(
                "MR head changed before checked-head merge"
            )
        return self.gitlab.merge(run, mr_iid, checked_head)

    def flush_outbox(self) -> None:
        if not hasattr(self, "_outbox_lock"):
            self._outbox_lock = threading.Lock()
        if not self._outbox_lock.acquire(blocking=False):
            return
        try:
            self._flush_outbox_once()
        finally:
            self._outbox_lock.release()

    def _flush_outbox_once(self) -> None:
        for item in self.store.pending_outbox():
            try:
                payload = json.loads(item["payload"])
                origin = FeishuOrigin.model_validate(payload["origin"])
                self.notifier.send(item["outbox_key"], origin, payload["text"])
                self.store.finish_outbox(item["outbox_key"])
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValidationError,
                DependencyContractError,
            ) as exc:
                self.store.fail_outbox(
                    item["outbox_key"],
                    str(exc),
                    error_class="dependency_contract",
                    permanent=True,
                )
            except DependencyError as exc:
                self.store.fail_outbox(
                    item["outbox_key"],
                    str(exc),
                    initial_backoff_seconds=(
                        self.config.outbox_backoff_initial_seconds
                    ),
                    maximum_backoff_seconds=(
                        self.config.outbox_backoff_max_seconds
                    ),
                    error_class=exc.error_class,
                )
            except Exception as exc:  # noqa: BLE001 - durable outbox boundary
                self.store.fail_outbox(
                    item["outbox_key"],
                    str(exc),
                    initial_backoff_seconds=(
                        self.config.outbox_backoff_initial_seconds
                    ),
                    maximum_backoff_seconds=(
                        self.config.outbox_backoff_max_seconds
                    ),
                )

    def _continue_abort(self, run_key: str) -> bool:
        if not self._begin_abort(run_key):
            return False
        try:
            self._continue_abort_once(run_key)
            return True
        finally:
            self._finish_abort(run_key)

    def _continue_abort_once(self, run_key: str) -> None:
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
        merged_head: str | None = None
        merge_commit_sha: str | None = None
        if terminal == "completed_before_abort":
            _, merged_head, merge_commit_sha = self._merged_mr_identity(mr)
        self.store.finish_abort(
            run_key,
            terminal,
            checked_head=merged_head,
            merge_commit_sha=merge_commit_sha,
        )
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

    def _dependency_is_open(self, dependency: str) -> bool:
        store = getattr(self, "store", None)
        if store is None or not hasattr(store, "open_dependency_outages"):
            return False
        return any(
            outage["dependency"] == dependency
            for outage in store.open_dependency_outages()
        )

    def _handle_dependency_error(
        self,
        run_key: str,
        error: DependencyError,
    ) -> dict:
        if isinstance(error, DependencyContractError):
            raise error
        context = error.context
        dependency = self._dependency_scope(run_key, error)
        outage = self.store.record_dependency_failure(
            dependency,
            self._error_text(error),
            initial_backoff_seconds=(
                self.config.dependency_backoff_initial_seconds
            ),
            maximum_backoff_seconds=self.config.dependency_backoff_max_seconds,
            error_class=error.error_class,
            endpoint=context.endpoint,
            retry_after_seconds=context.retry_after_seconds,
        )
        self.store.associate_outage_run(
            dependency,
            str(outage["outage_id"]),
            run_key,
        )
        if (
            context.dependency == "gitlab"
            and dependency != "gitlab"
            and isinstance(error, DependencyTransientError)
        ):
            corroborating_runs = {
                affected_run
                for scoped_outage in self.store.open_dependency_outages()
                if str(scoped_outage["dependency"]).startswith("gitlab:")
                and scoped_outage["dependency"] != "gitlab:request"
                and scoped_outage["error_class"]
                == DependencyTransientError.error_class
                for affected_run in self.store.outage_run_keys(
                    str(scoped_outage["dependency"]),
                    str(scoped_outage["outage_id"]),
                )
            }
            if (
                len(corroborating_runs)
                >= self.config.dependency_circuit_failure_threshold
            ):
                global_outage = self.store.record_dependency_failure(
                    "gitlab",
                    self._error_text(error),
                    initial_backoff_seconds=(
                        self.config.dependency_backoff_initial_seconds
                    ),
                    maximum_backoff_seconds=(
                        self.config.dependency_backoff_max_seconds
                    ),
                    error_class=error.error_class,
                    endpoint=context.endpoint,
                    retry_after_seconds=context.retry_after_seconds,
                )
                for affected_run in corroborating_runs:
                    self.store.associate_outage_run(
                        "gitlab",
                        str(global_outage["outage_id"]),
                        affected_run,
                    )
        control = self.store.run_control(run_key)
        if control and control["state"] in {
            "active",
            "merge_wait",
            "dependency_degraded",
        }:
            self.store.transition_run(
                run_key,
                expected_states={str(control["state"])},
                new_state="dependency_degraded",
                reason=(
                    f"{context.dependency}:{error.error_class}:"
                    f"{context.error_code or 'unavailable'}"
                ),
                expected_version=int(control["state_version"]),
                next_retry_at=int(outage["next_retry_at"]),
            )
        elif control and control["state"] in {
            "abort_requested",
            "aborting",
        }:
            self.store.transition_run(
                run_key,
                expected_states={str(control["state"])},
                new_state=str(control["state"]),
                reason=(
                    f"{context.dependency}:{error.error_class}:"
                    f"{context.error_code or 'unavailable'}"
                ),
                expected_version=int(control["state_version"]),
                next_retry_at=int(outage["next_retry_at"]),
            )
        self._enqueue_dependency_outage(run_key, outage)
        self.last_reconcile_error = (
            f"{context.dependency}:{error.error_class}:{self._error_text(error)}"
        )
        return outage

    @staticmethod
    def _dependency_scope(
        run_key: str | None,
        error: DependencyError,
    ) -> str:
        dependency = error.context.dependency
        if dependency == "gitlab" and isinstance(
            error,
            (DependencyAuthError, DependencyRateLimitedError),
        ):
            return dependency
        return (
            f"{dependency}:{run_key}"
            if run_key
            else f"{dependency}:request"
        )

    def _record_request_dependency_error(
        self,
        run_key: str | None,
        error: DependencyError,
    ) -> dict:
        if run_key and self.store.run_control(run_key) is not None:
            return self._handle_dependency_error(run_key, error)
        context = error.context
        dependency = self._dependency_scope(None, error)
        outage = self.store.record_dependency_failure(
            dependency,
            self._error_text(error),
            initial_backoff_seconds=(
                self.config.dependency_backoff_initial_seconds
            ),
            maximum_backoff_seconds=self.config.dependency_backoff_max_seconds,
            error_class=error.error_class,
            endpoint=context.endpoint,
            retry_after_seconds=context.retry_after_seconds,
        )
        self.last_reconcile_error = (
            f"{context.dependency}:{error.error_class}:"
            f"{self._error_text(error)}"
        )
        return outage

    def _record_run_exception(
        self,
        run_key: str,
        error: Exception,
    ) -> None:
        control = self.store.run_control(run_key)
        if control and control["state"] in {
            "active",
            "dependency_degraded",
            "merge_wait",
        }:
            self.store.set_run_exception(run_key, self._error_text(error))
            self._enqueue_controller_failure(run_key, error)

    def _recover_gitlab_circuit(self) -> None:
        outages = {
            str(item["dependency"]): item
            for item in self.store.open_dependency_outages()
        }
        outage = outages.get("gitlab")
        if outage is None:
            return
        now = int(time.time())
        if int(outage["next_retry_at"]) > now:
            return
        self.store.mark_dependency_half_open("gitlab")
        try:
            self.gitlab.health()
        except DependencyError as exc:
            updated = self.store.record_dependency_failure(
                "gitlab",
                self._error_text(exc),
                initial_backoff_seconds=(
                    self.config.dependency_backoff_initial_seconds
                ),
                maximum_backoff_seconds=(
                    self.config.dependency_backoff_max_seconds
                ),
                error_class=exc.error_class,
                endpoint=exc.context.endpoint,
                retry_after_seconds=exc.context.retry_after_seconds,
            )
            for run_key in self.store.outage_run_keys(
                "gitlab",
                str(updated["outage_id"]),
            ):
                control = self.store.run_control(run_key)
                if control and control["state"] == "dependency_degraded":
                    self.store.transition_run(
                        run_key,
                        expected_states={"dependency_degraded"},
                        new_state="dependency_degraded",
                        reason=(
                            f"gitlab:{exc.error_class}:"
                            f"{exc.context.error_code or 'unavailable'}"
                        ),
                        expected_version=int(control["state_version"]),
                        next_retry_at=int(updated["next_retry_at"]),
                    )
                self._enqueue_dependency_outage(run_key, updated)
            return
        recovered = self.store.recover_dependency("gitlab")
        if recovered is None:
            return
        self._finish_dependency_recovery("gitlab", recovered)

    def _recover_run_dependency(self, run_key: str, dependency: str) -> None:
        """Close a due outage only after a real run operation succeeds."""
        now = int(time.time())
        scoped_dependency = f"{dependency}:{run_key}"
        outage = next(
            (
                item
                for item in self.store.open_dependency_outages()
                if item["dependency"] == scoped_dependency
                and int(item["next_retry_at"]) <= now
            ),
            None,
        )
        if outage is None:
            return
        associated = self.store.outage_run_keys(
            scoped_dependency,
            str(outage["outage_id"]),
        )
        if run_key not in associated:
            return
        recovered = self.store.recover_dependency(scoped_dependency)
        if recovered is not None:
            self._finish_dependency_recovery(scoped_dependency, recovered)

    def _recover_request_dependency(self, dependency: str) -> None:
        scoped_dependency = f"{dependency}:request"
        outage = next(
            (
                item
                for item in self.store.open_dependency_outages()
                if item["dependency"] == scoped_dependency
                and int(item["next_retry_at"]) <= int(time.time())
            ),
            None,
        )
        if outage is not None:
            self.store.recover_dependency(scoped_dependency)

    def _finish_dependency_recovery(
        self,
        dependency: str,
        recovered: dict,
    ) -> None:
        base_dependency = dependency.split(":", 1)[0]
        for run_key in self.store.outage_run_keys(
            dependency,
            str(recovered["outage_id"]),
        ):
            control = self.store.run_control(run_key)
            if control and control["state"] == "dependency_degraded":
                remaining = [
                    outage
                    for outage in self.store.open_dependency_outages()
                    if run_key
                    in self.store.outage_run_keys(
                        str(outage["dependency"]),
                        str(outage["outage_id"]),
                    )
                ]
                if remaining:
                    self.store.transition_run(
                        run_key,
                        expected_states={"dependency_degraded"},
                        new_state="dependency_degraded",
                        reason="dependency_recovery_partial",
                        expected_version=int(control["state_version"]),
                        next_retry_at=min(
                            int(item["next_retry_at"])
                            for item in remaining
                        ),
                    )
                else:
                    self.store.transition_run(
                        run_key,
                        expected_states={"dependency_degraded"},
                        new_state="active",
                        reason=f"{base_dependency}_dependency_recovered",
                        expected_version=int(control["state_version"]),
                    )
            self._enqueue_dependency_recovered(run_key, recovered)

    def _record_agent_lifecycle_event(
        self, managed: ManagedCard, event: EventRecord
    ) -> None:
        if managed.purpose != "work":
            return
        worker_session_id = None
        worker_pid = None
        if isinstance(event.payload, dict):
            candidate = event.payload.get("worker_session_id")
            if isinstance(candidate, str) and candidate.strip():
                worker_session_id = candidate
            pid_candidate = event.payload.get("worker_pid", event.payload.get("pid"))
            if isinstance(pid_candidate, int) and pid_candidate > 0:
                worker_pid = pid_candidate
        if hasattr(self.store, "record_card_runtime_event"):
            self.store.record_card_runtime_event(
                board=managed.board,
                card_id=managed.card_id,
                kind=event.kind,
                created_at=event.created_at,
                worker_session_id=worker_session_id,
                worker_pid=worker_pid,
                lease_seconds=self.config.worker_progress_lease_seconds,
            )
        started = event.kind in {"claimed", "started", "worker_started"}
        interrupted = event.kind in {
            "blocked",
            "crashed",
            "timed_out",
            "gave_up",
            "spawn_auto_blocked",
        }
        should_notify = (
            started
            and self.config.notification_level == NotificationLevel.VERBOSE
        ) or (
            interrupted
            and (
                self.config.notification_level == NotificationLevel.VERBOSE
                or event.kind in {"gave_up", "spawn_auto_blocked"}
            )
        )
        if not should_notify:
            return
        try:
            _, run = self._history(managed.run_key)
            task = self.reader.task(managed.board, managed.card_id)
        except (DependencyError, ControllerFatalError):
            # Do not advance the Kanban event cursor. The durable runtime write
            # and stable outbox key make replay safe after the dependency
            # recovers.
            raise
        except (ValidationError, ValueError, TypeError) as exc:
            self._record_run_exception(managed.run_key, exc)
            return
        if task is None:
            return
        runtime = (
            self.store.card_runtime(managed.board, managed.card_id)
            if hasattr(self.store, "card_runtime")
            else None
        )
        attempt = int((runtime or {}).get("attempt") or 1)
        state_version = int(
            self._run_control(run.run_key).get("state_version") or 1
        )
        if started:
            self._enqueue_progress(
                run,
                (
                    f"agent:{managed.card_id}:{attempt}:"
                    f"started:{state_version}"
                ),
                self._mention(run.origin)
                + "Agent 已开始工作。\n"
                f"run={run.run_key} stage={managed.stage} "
                f"iteration={managed.iteration} agent={task.assignee} "
                f"card={task.id}",
            )
        elif interrupted:
            self._enqueue_progress(
                run,
                (
                    f"agent:{managed.card_id}:{attempt}:"
                    f"interrupted-{event.kind}:"
                    f"{state_version}"
                ),
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
                try:
                    task = self.reader.task(
                        str(runtime["board"]),
                        str(runtime["card_id"]),
                    )
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                    break
                if task is None or task.status != "running":
                    continue
                try:
                    _, run = self._history(run_key)
                except DependencyContractError as exc:
                    self._record_run_exception(run_key, exc)
                    break
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                    break
                except ControllerFatalError:
                    raise
                except (ValidationError, ValueError, TypeError) as exc:
                    self._record_run_exception(run_key, exc)
                    break
                process_state = self._worker_process_state(runtime.get("worker_pid"))
                if process_state == "running":
                    self.store.update_worker_watchdog(
                        board=str(runtime["board"]),
                        card_id=str(runtime["card_id"]),
                        attempt_status="running_verified",
                        lease_seconds=self.config.worker_progress_lease_seconds,
                    )
                    continue
                if process_state != "exited":
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "evidence_insufficient",
                        "worker PID/session evidence is incomplete or inaccessible",
                    )
                    continue
                if (
                    not runtime.get("worker_session_id")
                    or runtime.get("profile") != task.assignee
                    or runtime.get("worktree") != run.workspace.worktree
                    or runtime.get("branch") != run.workspace.branch
                ):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "identity_mismatch",
                        "profile/session/worktree/branch evidence does not match",
                    )
                    continue
                if not hasattr(self.gitlab, "local_workspace_state"):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "workspace_probe_unavailable",
                        "local workspace probe is unavailable",
                    )
                    continue
                workspace = self.gitlab.local_workspace_state(run)
                workspace_head = str(workspace.get("head_sha") or "")
                if (
                    not workspace.get("ok")
                    or workspace.get("branch") != run.workspace.branch
                    or not workspace_head
                ):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        str(
                            workspace.get("error_code")
                            or "workspace_evidence_incomplete"
                        ),
                        "worktree or branch identity could not be verified",
                    )
                    continue
                if not hasattr(self.gitlab, "delivery_mr"):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "mr_probe_unavailable",
                        "delivery MR probe is unavailable",
                    )
                    continue
                try:
                    mr = self.gitlab.delivery_mr(
                        run,
                        (
                            int(runtime["mr_iid"])
                            if runtime.get("mr_iid")
                            else None
                        ),
                    )
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        exc.context.error_code or exc.error_class,
                        "MR/head evidence is temporarily unavailable",
                    )
                    break
                persisted_mr = runtime.get("mr_iid")
                persisted_head = str(runtime.get("head_sha") or "")
                if mr is None:
                    mr_evidence_matches = not persisted_mr and not persisted_head
                else:
                    live_iid = int(mr.get("iid") or 0)
                    live_head = str(mr.get("sha") or "")
                    mr_evidence_matches = bool(
                        live_iid
                        and live_head
                        and live_head == workspace_head
                        and (
                            not persisted_mr
                            or live_iid == int(persisted_mr)
                        )
                        and (
                            not persisted_head
                            or live_head == persisted_head
                        )
                    )
                if not mr_evidence_matches:
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "mr_head_mismatch",
                        "local and persisted MR/head evidence does not match GitLab",
                    )
                    continue
                redispatch_count = int(runtime.get("redispatch_count") or 0)
                if redispatch_count >= self.config.worker_redispatch_limit:
                    if not self._begin_reconcile(run_key):
                        continue
                    try:
                        if not self._watchdog_target_is_current(
                            run_key,
                            runtime,
                        ):
                            continue
                        self.kanban.abort_task(
                            run.workspace.board,
                            str(runtime["card_id"]),
                            "worker redispatch limit exhausted after "
                            "confirmed exit",
                        )
                        self._exception(
                            run,
                            str(runtime["card_id"]),
                            "worker redispatch budget exhausted after "
                            "confirmed exit; "
                            f"stage={runtime['stage']}; "
                            f"card={runtime['card_id']}; "
                            f"attempt={runtime.get('attempt')}; "
                            f"limit={self.config.worker_redispatch_limit}",
                        )
                    except DependencyContractError as exc:
                        self._record_run_exception(run_key, exc)
                    except DependencyError as exc:
                        self._handle_dependency_error(run_key, exc)
                    finally:
                        self._finish_reconcile(run_key)
                    continue
                if not self._begin_reconcile(run_key):
                    continue
                try:
                    if not self._watchdog_target_is_current(
                        run_key,
                        runtime,
                    ):
                        continue
                    self.kanban.redispatch_stale_worker(
                        run.workspace.board,
                        str(runtime["card_id"]),
                        "worker progress lease expired and PID is no longer alive",
                    )
                    self.store.update_worker_watchdog(
                        board=str(runtime["board"]),
                        card_id=str(runtime["card_id"]),
                        attempt_status="redispatch_requested",
                        lease_seconds=min(
                            self.config.worker_progress_lease_seconds,
                            300,
                        ),
                        reason="confirmed_worker_exit",
                        increment_redispatch=True,
                    )
                    if (
                        self.config.notification_level
                        == NotificationLevel.VERBOSE
                    ):
                        self._enqueue_progress(
                            run,
                            (
                                f"agent:{runtime['card_id']}:redispatch:"
                                f"{runtime.get('attempt')}:"
                                f"{redispatch_count + 1}:"
                                f"{self._run_control(run.run_key).get('state_version', 1)}"
                            ),
                            self._mention(run.origin)
                            + "Agent 失联证据已确认，Hermes 已请求有限重派。\n"
                            f"run={run_key} stage={runtime['stage']} "
                            f"card={runtime['card_id']} "
                            f"attempt={runtime.get('attempt')} "
                            f"redispatch={redispatch_count + 1}/"
                            f"{self.config.worker_redispatch_limit}",
                        )
                except DependencyContractError as exc:
                    self._record_run_exception(run_key, exc)
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                finally:
                    self._finish_reconcile(run_key)

    def _watchdog_target_is_current(
        self,
        run_key: str,
        observed: dict,
    ) -> bool:
        control = self.store.run_control(run_key)
        if control is None or control["state"] != "active":
            return False
        task = self.reader.task(
            str(observed["board"]),
            str(observed["card_id"]),
        )
        if task is None or task.status != "running":
            return False
        current = self.store.card_runtime(
            str(observed["board"]),
            str(observed["card_id"]),
        )
        if current is None:
            return False
        return all(
            current.get(field) == observed.get(field)
            for field in (
                "attempt",
                "worker_session_id",
                "worker_pid",
                "deadline_at",
            )
        )

    @staticmethod
    def _worker_process_state(worker_pid: object) -> str:
        if not isinstance(worker_pid, int) or worker_pid <= 0:
            return "unknown"
        try:
            os.kill(worker_pid, 0)
        except ProcessLookupError:
            return "exited"
        except (PermissionError, OSError):
            return "unknown"
        proc_stat = Path(f"/proc/{worker_pid}/stat")
        try:
            raw = proc_stat.read_text(encoding="utf-8", errors="replace")
            state = raw.rsplit(")", 1)[1].strip().split(maxsplit=1)[0]
            if state == "Z":
                return "exited"
        except (OSError, IndexError):
            pass
        return "running"

    def _enqueue_worker_lease_notice(
        self,
        run: RunRecord,
        runtime: dict,
        deadline: int,
        error_code: str,
        explanation: str,
    ) -> None:
        if self.config.notification_level != NotificationLevel.VERBOSE:
            return
        self.store.enqueue(
            (
                f"{run.run_key}:worker-lease:{runtime['card_id']}:"
                f"{runtime.get('attempt')}:{deadline}:{error_code}"
            ),
            run.run_key,
            "worker-lease-expired",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "Agent 超过进展租约，但自动重派证据不足。\n"
                f"run={run.run_key} stage={runtime['stage']} "
                f"card={runtime['card_id']} attempt={runtime.get('attempt')} "
                f"session={runtime.get('worker_session_id') or 'unknown'} "
                f"pid={runtime.get('worker_pid') or 'unknown'} "
                f"error_code={error_code}\n"
                f"evidence={explanation}; Controller 未终止或重派该 Agent。",
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
        runtime = (
            self.store.card_runtime(
                item.managed.board,
                item.managed.card_id,
            )
            if hasattr(self.store, "card_runtime")
            else None
        )
        attempt = int((runtime or {}).get("attempt") or 1)
        self._enqueue_progress(
            run,
            (
                f"agent:{item.task.id}:{attempt}:"
                f"completed-accepted:{metadata.outcome.value}:"
                f"{self._run_control(run.run_key).get('state_version', 1)}"
            ),
            self._mention(run.origin)
            + "Agent completed / accepted。\n"
            f"run={run.run_key} stage={metadata.stage.value} "
            f"iteration={metadata.iteration} agent={item.task.assignee} "
            f"card={item.task.id} outcome={metadata.outcome.value} "
            f"duration_seconds={duration if duration is not None else 'unknown'}",
        )

    def _enqueue_agent_completion_rejected(
        self,
        run: RunRecord,
        item: HistoryItem,
        reason: str,
    ) -> None:
        if (
            getattr(self, "config", None) is None
            or self.config.notification_level != NotificationLevel.VERBOSE
        ):
            return
        runtime = (
            self.store.card_runtime(
                item.managed.board,
                item.managed.card_id,
            )
            if hasattr(self.store, "card_runtime")
            else None
        )
        attempt = int((runtime or {}).get("attempt") or 1)
        self._enqueue_progress(
            run,
            (
                f"agent:{item.task.id}:{attempt}:"
                f"completed-rejected:"
                f"{self._run_control(run.run_key).get('state_version', 1)}"
            ),
            self._mention(run.origin)
            + "Agent completed / rejected。\n"
            f"run={run.run_key} stage={item.managed.stage} "
            f"iteration={item.managed.iteration} agent={item.task.assignee} "
            f"card={item.task.id} reason={reason[:700]}",
        )

    def health(self, probe: str = "readiness") -> dict:
        if probe not in {"liveness", "readiness"}:
            raise ValueError(f"unsupported health probe {probe}")
        data = self.store.health()
        for outage in data["dependency_outages"]:
            outage["circuit_open"] = (
                outage["dependency"] == "gitlab"
                or int(outage["failures"])
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
                maximum = self.reader.max_event_id(board)
                cursor = int(data["event_cursors"].get(board, 0))
                board_health[board] = {
                    "ok": True,
                    "max_event_id": maximum,
                    "controller_event_cursor": cursor,
                    "event_lag": max(0, maximum - cursor),
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
                "last_poll_at": self.last_poll_at,
                "last_poll_error": self.last_poll_error,
                "reconcile_fresh": reconcile_fresh,
                "reconcile_delay_seconds": (
                    max(0, now - self.last_reconcile_at)
                    if self.last_reconcile_at is not None
                    else None
                ),
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
        self._assert_reconcile_mutable(run.run_key)
        full_history, _ = self._history(run.run_key)
        frozen_baselines = self._frozen_baselines(full_history, run)
        mr = self.gitlab.delivery_mr(run)
        self._assert_reconcile_mutable(run.run_key)
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
        try:
            self._assert_reconcile_mutable(run.run_key)
        except ReconcileSuperseded:
            try:
                self.kanban.abort_task(
                    run.workspace.board,
                    task.id,
                    "run state changed before card publication",
                )
            except Exception:
                LOG.exception(
                    "failed to cancel superseded card %s for run %s",
                    task.id,
                    run.run_key,
                )
            raise
        self.store.register_card_attempt(
            board=run.workspace.board,
            card_id=task.id,
            profile=assignee,
            dispatch_key=key,
            worktree=run.workspace.worktree,
            branch=run.workspace.branch,
        )
        if not publish:
            return task
        try:
            return self._ensure_work_published(run, task)
        except ReconcileSuperseded:
            try:
                self.kanban.abort_task(
                    run.workspace.board,
                    task.id,
                    "run state changed during card publication",
                )
            except Exception:
                LOG.exception(
                    "failed to cancel published stale card %s for run %s",
                    task.id,
                    run.run_key,
                )
            raise

    def _ensure_work_published(self, run: RunRecord, task: TaskRecord) -> TaskRecord:
        control = self._assert_reconcile_mutable(run.run_key)
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
            run_key=run.run_key,
            expected_state_version=int(control.get("state_version") or 1),
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

    def _validate_worker_attempt(self, item: HistoryItem) -> None:
        if not hasattr(self, "store") or not hasattr(
            self.store,
            "card_runtime",
        ):
            return
        runtime = self.store.card_runtime(
            item.managed.board,
            item.managed.card_id,
        )
        if runtime is None or not runtime.get("worker_session_id"):
            return
        raw = item.task.latest_metadata
        completed_session = (
            raw.get("worker_session_id") if isinstance(raw, dict) else None
        )
        if completed_session != runtime["worker_session_id"]:
            raise ValueError(
                "completion worker_session_id does not match current attempt"
            )

    def _validate_semantic_gate(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        if metadata.gate_phase is None:
            return
        if (
            metadata.gate_reviewed_at is None
            or metadata.gate_reviewed_at.timestamp() > time.time() + 300
        ):
            raise ValueError("gate reviewed_at is missing or in the future")
        if metadata.stage == Stage.TASKS_REVIEW:
            if (
                sorted(metadata.gate_artifact_paths)
                != sorted(metadata.artifact_paths)
                or metadata.gate_artifact_commit_sha
                != metadata.artifact_commit_sha
                or metadata.gate_artifact_digest
                != metadata.artifact_digest
            ):
                raise ValueError(
                    "implementation_entry gate must bind the reviewed TASKS "
                    "artifact"
                )
        else:
            card = parse_card_body(item.task.body)
            tasks_baselines = [
                baseline
                for baseline in card.frozen_baselines
                if baseline.phase == "tasks"
            ]
            matching = [
                baseline
                for baseline in tasks_baselines
                if baseline.artifact_paths
                == sorted(metadata.gate_artifact_paths)
                and baseline.artifact_commit_sha
                == metadata.gate_artifact_commit_sha
                and baseline.artifact_digest == metadata.gate_artifact_digest
            ]
            if len(matching) != 1:
                raise ValueError(
                    "semantic gate must bind exactly one frozen TASKS baseline"
                )
        self.gitlab.validate_semantic_gate(run, metadata)

    @staticmethod
    def _validate_gate_reviewer(
        metadata: CompletionMetadata,
        gate_author: str,
    ) -> None:
        if (
            metadata.gate_reviewer is not None
            and metadata.gate_reviewer != gate_author
        ):
            raise ValueError(
                "semantic gate reviewer does not match GitLab gate author"
            )

    def _record_attempt_completion(
        self,
        item: HistoryItem,
        *,
        board: str,
        accepted: bool,
        mr_iid: int | None,
        head_sha: str | None,
        reason: str | None = None,
    ) -> None:
        if not hasattr(self, "store") or not hasattr(
            self.store,
            "record_attempt_completion",
        ):
            return
        self.store.record_attempt_completion(
            board=board,
            card_id=item.managed.card_id,
            accepted=accepted,
            mr_iid=mr_iid,
            head_sha=head_sha,
            reason=reason,
        )

    def _reject_agent_completion(
        self,
        run: RunRecord,
        item: HistoryItem,
        reason: str,
        *,
        mr_iid: int | None = None,
        head_sha: str | None = None,
    ) -> None:
        self._record_attempt_completion(
            item,
            board=item.managed.board,
            accepted=False,
            mr_iid=mr_iid,
            head_sha=head_sha,
            reason=reason,
        )
        self._enqueue_agent_completion_rejected(run, item, reason)

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
        self._assert_reconcile_mutable(run.run_key)
        self._reject_agent_completion(run, latest, reason)
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
        self._assert_reconcile_mutable(run.run_key)
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
        try:
            self._assert_reconcile_mutable(run.run_key)
        except ReconcileSuperseded:
            try:
                self.kanban.abort_task(
                    run.workspace.board,
                    task.id,
                    "run state changed while exception was being recorded",
                )
            except Exception:
                LOG.exception(
                    "failed to cancel stale exception card %s for run %s",
                    task.id,
                    run.run_key,
                )
            raise
        try:
            self.store.set_run_exception(run.run_key, reason)
        except ValueError:
            control = self._run_control(run.run_key)
            if str(control.get("state")) not in {
                "active",
                "dependency_degraded",
                "merge_wait",
                "exception",
            }:
                try:
                    self.kanban.abort_task(
                        run.workspace.board,
                        task.id,
                        "run state superseded exception transition",
                    )
                except Exception:
                    LOG.exception(
                        "failed to cancel stale exception card %s for run %s",
                        task.id,
                        run.run_key,
                    )
                raise ReconcileSuperseded(
                    f"exception_transition_superseded:{run.run_key}:"
                    f"{control.get('state')}"
                )
            raise
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
        *,
        run_key: str | None = None,
        expected_state_version: int | None = None,
        expected_head_sha: str | None = None,
    ) -> dict:
        if run_key is not None and expected_state_version is not None:
            control = self._assert_reconcile_mutable(run_key)
            if int(control.get("state_version") or 1) != expected_state_version:
                raise ReconcileSuperseded(
                    f"operation_state_version_changed:{run_key}:"
                    f"{expected_state_version}!="
                    f"{control.get('state_version')}"
                )
        previous = self.store.operation_result(
            key,
            kind,
            payload,
            expected_state_version=expected_state_version,
            expected_head_sha=expected_head_sha,
        )
        if previous is not None:
            return previous
        try:
            result = action()
            self.store.finish_operation(key, result)
            if run_key is not None and expected_state_version is not None:
                control = self._assert_reconcile_mutable(run_key)
                if int(control.get("state_version") or 1) != expected_state_version:
                    raise ReconcileSuperseded(
                        f"discarded_stale_operation_result:{run_key}:{kind}"
                    )
            return result
        except ReconcileSuperseded:
            raise
        except DependencyError as exc:
            # A timeout, rate limit, or authentication failure can arrive
            # after the remote side committed the mutation. Preserve the
            # uncertainty and let the operation-specific reconcile read back
            # external state before retrying.
            self.store.mark_operation_uncertain(key, str(exc))
            raise
        except MergeBlocked as exc:
            self.store.block_operation(key, str(exc))
            raise
        except CheckedHeadConflict as exc:
            self.store.supersede_operation(key, str(exc))
            raise
        except (RunPolicyError, ValidationError, ValueError, TypeError) as exc:
            self.store.fail_operation(key, str(exc))
            raise
        except Exception as exc:
            self.store.mark_operation_uncertain(key, str(exc))
            raise ControllerFatalError(
                f"operation_failed_without_classification:{kind}:{exc}"
            ) from exc

    def _enqueue_progress(
        self,
        run: RunRecord,
        event_key: str,
        text: str,
        *,
        allow_minimal: bool = False,
    ) -> None:
        config = getattr(self, "config", None)
        if (
            config is not None
            and config.notification_level == NotificationLevel.MINIMAL
            and not allow_minimal
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
    def _valid_human_block(
        fields: dict[str, str],
        *,
        stage: Stage | None = None,
    ) -> bool:
        if (
            REQUIRED_HUMAN_BLOCK_FIELDS - fields.keys()
            or fields.get("kind") not in ALLOWED_HUMAN_BLOCK_KINDS
        ):
            return False
        if fields.get("kind") in {"environment", "destructive_approval"}:
            if GATED_HUMAN_BLOCK_FIELDS - fields.keys():
                return False
            phase = fields.get("gate_phase")
            if phase not in {
                "implementation_entry",
                "implementation_completion",
                "migration_execution",
                "deployment_entry",
                "release_acceptance",
            }:
                return False
            if not fields.get("requirement_ids") or not fields.get(
                "contract_refs"
            ):
                return False
            # This controller owns repository implementation and checked-head
            # merge only. Target migration/deployment/release facts may be
            # recorded in frozen artifacts, but workers cannot elevate them
            # into a blocker for local implementation or code verification.
            if stage in {
                Stage.IMPLEMENT,
                Stage.TEST,
                Stage.CODE_REVIEW,
            } and phase in {
                "migration_execution",
                "deployment_entry",
                "release_acceptance",
            }:
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

    def _finalize_merged(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        mr: dict,
        *,
        operation_key: str | None = None,
    ) -> None:
        mr_iid, checked_head, merge_sha = self._merged_mr_identity(mr)
        self._complete_merged_run(
            run,
            history,
            mr,
            mr_iid=mr_iid,
            checked_head=checked_head,
            merge_sha=merge_sha,
            operation_key=operation_key,
        )

    @staticmethod
    def _merged_mr_identity(mr: dict) -> tuple[int, str, str | None]:
        try:
            mr_iid = int(mr.get("iid") or 0)
        except (TypeError, ValueError) as exc:
            raise DependencyContractError(
                "merged MR response lacks a valid iid/checked head",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="merge_requests",
                    error_code="invalid_merged_mr_identity",
                ),
            ) from exc
        checked_head = str(mr.get("sha") or "")
        merge_sha = str(mr.get("merge_commit_sha") or "") or None
        if mr_iid <= 0 or not (
            len(checked_head) == 40
            and all(
                character in "0123456789abcdef"
                for character in checked_head
            )
        ) or (
            merge_sha is not None
            and (
                len(merge_sha) != 40
                or any(
                    character not in "0123456789abcdef"
                    for character in merge_sha
                )
            )
        ):
            raise DependencyContractError(
                "merged MR response lacks a valid iid/checked head",
                context=ErrorContext(
                    dependency="gitlab",
                    endpoint="merge_requests",
                    error_code="invalid_merged_mr_identity",
                ),
            )
        return mr_iid, checked_head, merge_sha

    def _complete_merged_run(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        mr: dict,
        *,
        mr_iid: int,
        checked_head: str,
        merge_sha: str | None,
        operation_key: str | None = None,
    ) -> None:
        managed_merge = False
        if operation_key is None and checked_head:
            operation_key = f"{run.run_key}:merge:{checked_head}"
        if operation_key:
            operation = self.store.operation_record(operation_key)
            operation_result: dict = {}
            if operation is not None and operation.get("result"):
                try:
                    decoded_result = json.loads(str(operation["result"]))
                except (TypeError, json.JSONDecodeError):
                    decoded_result = {}
                if isinstance(decoded_result, dict):
                    operation_result = decoded_result
            if (
                operation is not None
                and operation["status"] == "done"
                and operation.get("expected_head_sha") == checked_head
                and operation_result.get(CONTROLLER_MERGE_SUBMITTED_FIELD)
                is True
            ):
                managed_merge = True

        compliance = "unverified"
        compliance_reason = (
            "merged_without_controller_checked_head_operation"
            if not managed_merge
            else "merged_without_current_controller_gate_evidence"
        )
        test = self._latest_valid_pass(history, Stage.TEST)
        review = self._latest_valid_pass(history, Stage.CODE_REVIEW)
        if (
            managed_merge
            and checked_head
            and test is not None
            and review is not None
            and test.head_sha == checked_head
            and review.head_sha == checked_head
            and test.mr_iid == mr_iid
            and review.mr_iid == mr_iid
        ):
            try:
                test_author = self.gitlab.validate_gate(run, test)
                review_author = self.gitlab.validate_gate(run, review)
                if test_author == review_author:
                    compliance_reason = "test_and_code_review_author_must_differ"
                else:
                    compliance = "verified"
                    compliance_reason = "checked_head_gates_verified"
            except DependencyError:
                raise
            except (ValueError, TypeError) as exc:
                compliance_reason = f"gate_evidence_invalid:{exc}"

        control = self._run_control(run.run_key)
        if control["state"] in {"abort_requested", "aborting"}:
            self.store.clear_merge_wait(run.run_key)
            self.store.finish_abort(
                run.run_key,
                "completed_before_abort",
                checked_head=checked_head or None,
                merge_commit_sha=merge_sha,
            )
            self.store.enqueue(
                f"{run.run_key}:completed-before-abort:{merge_sha or mr_iid}",
                run.run_key,
                "completed-before-abort",
                {
                    "origin": run.origin.model_dump(mode="json"),
                    "text": self._mention(run.origin)
                    + "废止与合并发生竞争；重新核验后确认 MR 已合并。\n"
                    f"run={run.run_key} mr={mr.get('web_url')} "
                    f"head={checked_head or 'unknown'} "
                    f"merge={merge_sha or 'unknown'}\n"
                    "state=completed_before_abort",
                },
            )
            return
        if control["state"] in TERMINAL_RUN_STATES:
            return
        self.store.clear_merge_wait(run.run_key)
        self.store.mark_completed(
            run.run_key,
            external=not managed_merge,
            compliance=compliance,
            checked_head=checked_head or "unknown",
            merge_commit_sha=merge_sha,
            reason=compliance_reason,
        )
        if compliance == "verified":
            self._enqueue_success(run, mr)
            return
        self.store.enqueue(
            f"{run.run_key}:merged-policy-violation:{merge_sha or mr_iid}",
            run.run_key,
            "merged-policy-violation",
            {
                "origin": run.origin.model_dump(mode="json"),
                "text": self._mention(run.origin)
                + "检测到流程已合并，但长期版 Controller 无法验证同一 head 的完整门禁证据。\n"
                f"run={run.run_key} mr={mr.get('web_url')} "
                f"head={checked_head or 'unknown'} merge={merge_sha or 'unknown'}\n"
                f"compliance=unverified reason={compliance_reason[:500]}\n"
                "状态已终止为 completed（source=external），不会再次调度或合并。",
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
        if (
            self.config.notification_level == NotificationLevel.MINIMAL
            and outage.get("error_class") != "dependency_auth"
        ):
            return
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
                f"run={run_key} "
                f"dependency={str(outage['dependency']).split(':', 1)[0]} "
                f"outage={outage_id} "
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
        if (
            self.config.notification_level == NotificationLevel.MINIMAL
            and outage.get("error_class") != "dependency_auth"
        ):
            return
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
                f"run={run_key} "
                f"dependency={str(outage['dependency']).split(':', 1)[0]} "
                f"outage={outage_id} "
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
