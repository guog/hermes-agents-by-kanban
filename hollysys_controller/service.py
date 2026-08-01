from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

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
from .git_auth import profile_preflight, summarize_profile_preflight
from .gitlab import (
    CONTROLLER_MERGE_SUBMITTED_FIELD,
    CheckedHeadConflict,
    GitLabClient,
    StartFacts,
)
from .kanban import (
    EventRecord,
    KanbanCLI,
    KanbanReader,
    TaskRecord,
    parse_card_body,
    parse_run_body,
)
from .messages import (
    escape_markdown,
    format_agent,
    format_attempt,
    format_duration,
    format_event,
    format_outcome,
    format_stage,
    gitlab_link_label,
    human_summary,
    inline_code,
    markdown_link,
    markdown_payload,
    render_message,
    short_sha,
)
from .models import (
    AbortConfirmRequest,
    AbortRequest,
    ArtifactBaseline,
    BaselineDisposition,
    CardRecord,
    CompletionMetadata,
    CompletionValidationRequest,
    DeliveryBinding,
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
from .validators import validate_task_documents
from .workflow import (
    DOCUMENT_REVIEW_FOR_PRODUCER,
    PHASE_FOR_STAGE,
    PRODUCER_FOR_PHASE,
    protocol_retry_allowed,
    route_completion,
)

ACTIVE_STATUSES = {"triage", "todo", "ready", "running", "blocked"}
LOG = logging.getLogger(__name__)
CANONICAL_SCRATCH_ROOT = PurePosixPath("/opt/data/scratch")
TERMINAL_EVENT_KINDS = {
    "completed",
    "blocked",
    "crashed",
    "rate_limited",
    "timed_out",
    "gave_up",
    "spawn_auto_blocked",
    "status",
    "promoted_manual",
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

    def _prepare_card_scratch_dir(self, record: CardRecord) -> Path:
        """Create the published attempt scratch directory before dispatch."""
        canonical = PurePosixPath(record.scratch_dir)
        try:
            relative = canonical.relative_to(CANONICAL_SCRATCH_ROOT)
        except ValueError as exc:
            raise RunPolicyError("unsafe_card_scratch_dir") from exc

        root = self.config.hermes_home / "scratch"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunPolicyError("scratch_root_unavailable") from exc
        if root.is_symlink() or not root.is_dir():
            raise RunPolicyError("unsafe_scratch_root")

        current = root
        for part in relative.parts:
            current /= part
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError as exc:
                raise RunPolicyError("scratch_dir_unavailable") from exc
            if current.is_symlink() or not current.is_dir():
                raise RunPolicyError("unsafe_scratch_component")

        resolved_root = root.resolve(strict=True)
        resolved = current.resolve(strict=True)
        if resolved_root not in resolved.parents:
            raise RunPolicyError("unsafe_card_scratch_dir")
        return current

    def start(self, raw: dict) -> dict:
        if getattr(self.config, "controller_mode", "active") != "active":
            raise ValueError("controller_preflight_mode")
        request = StartRequest.model_validate(raw)
        request_key = f"start:{request.message_id}"
        if not self._begin_request_execution(request_key):
            return self.start_request_status(raw)
        try:
            return self._start(raw)
        finally:
            self._finish_request_execution(request_key)

    def start_request_status(self, raw: dict) -> dict:
        """Return a non-mutating snapshot for an accepted, in-flight start."""
        request = StartRequest.model_validate(raw)
        request_key = f"start:{request.message_id}"
        persisted = self.store.request(request_key)
        if persisted is None:
            return {
                "request_key": request_key,
                "request_status": "starting",
                "run_key": None,
                "phase": "request-validation",
                "stage": "run-initialization",
                "active_card": None,
            }
        if persisted["status"] == "done" and persisted.get("response"):
            return dict(persisted["response"])
        if persisted["status"] == "failed":
            raise RuntimeError(
                persisted.get("error") or f"request {request_key} failed"
            )
        run_key = str(persisted.get("run_key") or "")
        if run_key and self.store.run_record(run_key) is not None:
            result = self.status_summary(run_key)
            result["request_key"] = request_key
            result["request_status"] = "running"
            return result
        return {
            "request_key": request_key,
            "request_status": "running",
            "run_key": None,
            "phase": "request-validation",
            "stage": "run-initialization",
            "active_card": None,
        }

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
            request_state = self.store.request(key)
            persisted_run_key = (
                str(request_state.get("run_key") or "")
                if request_state is not None
                else ""
            )
            persisted_run = (
                self.store.run_record(persisted_run_key)
                if persisted_run_key
                else None
            )
            if persisted_run is not None:
                facts = StartFacts(
                    run=persisted_run,
                    base_sha=persisted_run.workspace.repository_base_sha,
                )
            else:
                facts = self.gitlab.validate_start(
                    prd_blob_url=str(request.prd_blob_url),
                    prd_mr_url=str(request.prd_mr_url),
                    origin=origin,
                )
            run = facts.run
            dependency_run_key = run.run_key
            self.store.save_run(run)
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
            response = self._initialize_run(run, facts.base_sha)
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

    def _initialize_run(self, run: RunRecord, base_sha: str) -> dict:
        """Create the durable external run root after identity is persisted.

        Every mutating preparation step is idempotent so a human-authorized
        recovery can safely resume a failure that happened before the Kanban
        root card was registered.
        """
        self._operation(
            f"{run.run_key}:remote-branch",
            "remote-branch",
            {
                "run_key": run.run_key,
                "branch": run.workspace.branch,
                "base_sha": base_sha,
            },
            lambda: self.gitlab.create_delivery_branch(run),
        )
        self._operation(
            f"{run.run_key}:workspace",
            "workspace",
            {"run_key": run.run_key, "base_sha": base_sha},
            lambda: (
                self.gitlab.ensure_workspace(run, base_sha)
                or {"worktree": run.workspace.worktree}
            ),
        )
        accepted_preflight = self.store.deployment_preflight()
        if (
            accepted_preflight is not None
            and accepted_preflight.get("ok")
            and accepted_preflight.get("deep")
        ):
            self._operation(
                f"{run.run_key}:offline-caches",
                "offline-caches",
                {
                    "run_key": run.run_key,
                    "worktree": run.workspace.worktree,
                },
                lambda: self._prepare_offline_caches(run),
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
            lambda: self.kanban.complete_root(run, root.id)
            or {"card_id": root.id},
        )
        first = self._create_work(run, Stage.SPEC_WRITE, root.id)
        self._enqueue_progress(
            run,
            "run-accepted",
            self._render_notification(
                run,
                icon="ℹ️",
                title="已受理 PRD 自动交付",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("阶段", format_stage(Stage.SPEC_WRITE)),
                    ("Agent", format_agent(first.assignee)),
                    ("Card", inline_code(first.id)),
                ],
            ),
        )
        self._enqueue_phase_started(run, Phase.SPEC, first)
        return {
            "run_key": run.run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "project": run.project.project_path,
            "stage": Stage.SPEC_WRITE.value,
            "active_card": first.id,
            "board": run.workspace.board,
            "worktree": run.workspace.worktree,
        }

    def _prepare_offline_caches(self, run: RunRecord) -> dict:
        command = self.config.offline_cache_command
        if (
            command.is_symlink()
            or not command.is_file()
            or not os.access(command, os.X_OK)
        ):
            raise RunPolicyError("tool_unavailable:offline_cache_preparer")
        worktree = Path(run.workspace.worktree).resolve()
        projects_root = self.config.projects_root.resolve()
        if worktree == projects_root or projects_root not in worktree.parents:
            raise RunPolicyError("unsafe_offline_cache_workspace")
        environment = os.environ.copy()
        environment.update(
            {
                "NPM_CONFIG_CACHE": str(
                    self.config.hermes_home / "cache" / "npm"
                ),
                "NUGET_PACKAGES": str(
                    self.config.hermes_home / "cache" / "nuget"
                ),
                "NPM_CONFIG_OFFLINE": "false",
            }
        )
        result = subprocess.run(
            [str(command), str(worktree)],
            cwd=worktree,
            env=environment,
            text=True,
            capture_output=True,
            timeout=self.config.offline_cache_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            summary = (
                result.stderr.strip() or result.stdout.strip()
            )[-1000:]
            raise RunPolicyError(
                f"offline_cache_prepare_failed:{result.returncode}:{summary}"
            )
        try:
            payload = json.loads(result.stdout.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RunPolicyError(
                "offline_cache_prepare_invalid_output"
            ) from exc
        if payload.get("ok") is not True:
            raise RunPolicyError(
                "offline_cache_prepare_rejected:"
                + str(payload.get("error_code") or "unknown")
            )
        return payload

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
            mr = self._delivery_mr(run)
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
        if (
            hasattr(self.store, "cards_for_run")
            and not self.store.cards_for_run(run_key)
        ):
            return self._initialization_status_summary(run_key)
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
        binding = (
            self.store.delivery_binding(run_key)
            if hasattr(self.store, "delivery_binding")
            else None
        )
        reconcile_intents = (
            self.store.reconcile_intents(run_key)
            if hasattr(self.store, "reconcile_intents")
            else []
        )
        controller_cursor = int(
            store_health["event_cursors"].get(run.workspace.board, 0)
        )
        kanban_max_event_id = self.reader.max_event_id(run.workspace.board)

        return {
            "run_key": run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "started_at": run.started_at.isoformat(),
            "provenance": run.provenance,
            "control": control,
            "transition_pending": control_state == "transition_pending",
            "human_blocked": control_state == "human_blocked",
            "retry_wait": control_state == "retry_wait",
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
            "attempt_timeline": (
                self.store.attempts_for_run(run_key)
                if hasattr(self.store, "attempts_for_run")
                else []
            ),
            "delivery_binding": (
                binding.model_dump(mode="json")
                if binding is not None
                else None
            ),
            "reconcile": {
                "pending": bool(reconcile_intents),
                "queue": reconcile_intents,
                "current_step": (
                    reconcile_intents[0].get("current_step")
                    if reconcile_intents
                    else None
                ),
            },
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

    def _initialization_status_summary(self, run_key: str) -> dict:
        """Describe a persisted Run that has not registered its root card yet."""
        run = self.store.run_record(run_key)
        if run is None:
            raise ValueError(f"unknown run {run_key}")
        control = self._run_control(run_key)
        control_state = str(control.get("state") or "active")
        store_health = self.store.health()
        operations = []
        for suffix in (
            "remote-branch",
            "workspace",
            "offline-caches",
            "board",
            "complete-root",
        ):
            operation = self.store.operation_record(f"{run_key}:{suffix}")
            if operation is None:
                continue
            created_at = int(operation.get("created_at") or 0)
            updated_at = int(operation.get("updated_at") or created_at)
            operations.append(
                {
                    "kind": operation["kind"],
                    "status": operation["status"],
                    "attempts": int(operation.get("attempts") or 0),
                    "duration_seconds": max(0, updated_at - created_at),
                    "error": operation.get("error"),
                }
            )
        initialization_error = next(
            (
                str(operation["error"]).strip()[:1000]
                for operation in reversed(operations)
                if operation["status"] in {"failed", "uncertain", "blocked"}
                and operation.get("error")
            ),
            None,
        )
        controller_cursor = int(
            store_health["event_cursors"].get(run.workspace.board, 0)
        )
        return {
            "run_key": run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "started_at": run.started_at.isoformat(),
            "provenance": run.provenance,
            "control": control,
            "transition_pending": control_state == "transition_pending",
            "human_blocked": control_state == "human_blocked",
            "retry_wait": control_state == "retry_wait",
            "notification_level": self.config.notification_level.value,
            "phase": (
                "exception"
                if control_state == "exception"
                else "initialization"
            ),
            "stage": "run-initialization",
            "active_card": None,
            "attempts": {},
            "review_attempts": {},
            "review_remaining": {},
            "code_modifications": {
                "used": 0,
                "remaining": self.config.code_modification_limit,
                "limit": self.config.code_modification_limit,
            },
            "protocol_failures": {},
            "blocked": (
                initialization_error
                or control.get("last_transition_reason")
                if control_state == "exception"
                else None
            ),
            "merge_wait": None,
            "worker_runtime": [],
            "attempt_timeline": [],
            "delivery_binding": None,
            "reconcile": {
                "pending": False,
                "queue": [],
                "current_step": None,
            },
            "initialization": {
                "completed": False,
                "operations": operations,
            },
            "board": run.workspace.board,
            "worktree": run.workspace.worktree,
            "repository_base_sha": run.workspace.repository_base_sha,
            "snapshot": {
                "authority": "controller-store",
                "gitlab_audit": "not_requested",
                "controller_event_cursor": controller_cursor,
                "kanban_max_event_id": controller_cursor,
                "event_lag": 0,
                "event_lag_warning": False,
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
            checks["gitlab_token"] = {
                "ok": True,
                "source": str(self.config.controller_token_file),
                "identity": "dispatcher-controller",
            }
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
        checks["projects_root"] = {
            "ok": (
                self.config.projects_root.is_dir()
                and os.access(
                    self.config.projects_root,
                    os.W_OK | os.X_OK,
                )
            ),
            "path": str(self.config.projects_root),
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
        checks["toolchain"] = self._toolchain_preflight()
        checks["offline_cache_preparer"] = {
            "ok": (
                self.config.offline_cache_command.is_file()
                and not self.config.offline_cache_command.is_symlink()
                and os.access(self.config.offline_cache_command, os.X_OK)
            ),
            "path": str(self.config.offline_cache_command),
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

    @staticmethod
    def _toolchain_preflight() -> dict:
        probes = {
            "jq": (["jq", "--version"], lambda value: value.startswith("jq-")),
            "node": (
                ["node", "--version"],
                lambda value: (
                    value.startswith("v")
                    and int(value[1:].split(".", 1)[0]) >= 22
                ),
            ),
            "npm": (
                ["npm", "--version"],
                lambda value: int(value.split(".", 1)[0]) >= 10,
            ),
            "dotnet": (
                ["dotnet", "--version"],
                lambda value: value == "8.0.423",
            ),
        }
        versions: dict[str, str] = {}
        errors: list[str] = []
        for name, (command, validator) in probes.items():
            try:
                completed = subprocess.run(
                    command,
                    text=True,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
                value = completed.stdout.strip()
                valid = completed.returncode == 0 and validator(value)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                value = "unavailable"
                valid = False
            versions[name] = value[:80]
            if not valid:
                errors.append(name)
        return {
            "ok": not errors,
            "versions": versions,
            "error_code": (
                "tool_unavailable:" + ",".join(errors) if errors else None
            ),
        }

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
                "profile_contract_changed_after_deep_preflight"
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

    def _managed_work(self, card_id: str) -> tuple[ManagedCard, HistoryItem, RunRecord]:
        matches = [
            card
            for run_key in self.store.run_keys()
            for card in self.store.cards_for_run(run_key)
            if card.card_id == card_id and card.purpose == "work"
        ]
        if len(matches) != 1:
            raise ValueError(
                "card_id must identify exactly one managed Hollysys work card"
            )
        managed = matches[0]
        history, run = self._history(managed.run_key)
        item = next(entry for entry in history if entry.task.id == card_id)
        return managed, item, run

    def publish_delivery(self, raw: dict) -> dict:
        card_id = str(raw.get("card_id") or "")
        head_sha = str(raw.get("head_sha") or "")
        description = str(raw.get("description") or "")
        if not card_id or not description.strip():
            raise ValueError("card_id and non-empty description are required")
        managed, item, run = self._managed_work(card_id)
        record = parse_card_body(item.task.body)
        if (
            managed.stage != Stage.SPEC_WRITE.value
            or managed.iteration != 1
            or record.mode != WorkMode.NORMAL
        ):
            raise ValueError(
                "only the first normal SPEC writer card may publish delivery"
            )
        if item.task.status not in ACTIVE_STATUSES:
            raise ValueError("publish-delivery card is no longer active")
        if self.store.delivery_binding(run.run_key) is not None:
            raise ValueError(f"delivery_already_bound:{run.run_key}")
        result = self._operation(
            f"{run.run_key}:publish-delivery",
            "publish-delivery",
            {
                "run_key": run.run_key,
                "card_id": card_id,
                "head_sha": head_sha,
                "description_digest": hashlib.sha256(
                    description.encode()
                ).hexdigest(),
            },
            lambda: self.gitlab.publish_delivery(
                run,
                head_sha=head_sha,
                description=description,
            ).model_dump(mode="json"),
            run_key=run.run_key,
            expected_state_version=int(
                self._run_control(run.run_key)["state_version"]
            ),
            expected_head_sha=head_sha,
        )
        binding = DeliveryBinding.model_validate(result)
        self.store.bind_delivery(run.run_key, binding)
        return {
            "ok": True,
            "run_key": run.run_key,
            "card_id": card_id,
            "delivery": binding.model_dump(mode="json"),
        }

    def card_context(self, raw: dict) -> dict:
        card_id = str(raw.get("card_id") or "")
        managed, item, run = self._managed_work(card_id)
        record = parse_card_body(item.task.body)
        binding = self.store.delivery_binding(run.run_key)
        live_mr = self._delivery_mr(run)
        workspace_state = self.gitlab.local_workspace_state(run)
        attempts = [
            attempt
            for attempt in self.store.attempts_for_run(run.run_key)
            if attempt["card_id"] == card_id
        ]
        runtime = self.store.card_runtime(managed.board, card_id)
        retry = len(attempts) > 1 or bool(
            runtime and int(runtime.get("redispatch_count") or 0) > 0
        )
        remote_head = (
            str(live_mr.get("sha") or "") if live_mr is not None else None
        )
        resume_mode = self._resume_mode(
            stage=Stage(managed.stage),
            retry=retry,
            workspace_state=workspace_state,
            remote_head=remote_head,
            expected_head=record.expected_head_sha,
        )
        current_attempt = attempts[-1] if attempts else None
        return {
            "protocol_version": "hollysys-controller/v4",
            "trusted": True,
            "card_id": card_id,
            "status": item.task.status,
            "run": {
                "run_key": run.run_key,
                "source_key": run.source_key,
                "run_generation": run.run_generation,
                "started_at": run.started_at.isoformat(),
                "provenance": run.provenance,
            },
            "stage": managed.stage,
            "iteration": managed.iteration,
            "mode": record.mode.value,
            "project": {
                "id": run.project.project_id,
                "path": run.project.project_path,
                "default_branch": run.project.default_branch,
            },
            "source": run.source.model_dump(mode="json"),
            "workspace": run.workspace.model_dump(mode="json"),
            "workspace_state": workspace_state,
            "expected_head_sha": record.expected_head_sha,
            "context_digest": record.context_digest,
            "scratch_dir": record.scratch_dir,
            "delivery": (
                {
                    **binding.model_dump(mode="json"),
                    "current_head_sha": str(live_mr.get("sha") or ""),
                    "state": str(live_mr.get("state") or ""),
                    "draft": bool(live_mr.get("draft", False)),
                }
                if binding is not None and live_mr is not None
                else None
            ),
            "frozen_baselines": [
                item.model_dump(mode="json")
                for item in record.frozen_baselines
            ],
            "repair_context": (
                record.repair_context.model_dump(mode="json")
                if record.repair_context is not None
                else None
            ),
            "resume_answer": record.resume_answer,
            "resume": {
                "mode": resume_mode,
                "attempt_count": len(attempts),
                "redispatch_count": (
                    int(runtime.get("redispatch_count") or 0)
                    if runtime is not None
                    else 0
                ),
                "current_attempt": (
                    {
                        key: current_attempt.get(key)
                        for key in (
                            "run_id",
                            "attempt",
                            "status",
                            "started_at",
                            "last_progress_at",
                            "terminal_reason",
                            "updated_at",
                        )
                    }
                    if current_attempt is not None
                    else None
                ),
                "remote_head_sha": remote_head,
                "remaining_protocol_steps": (
                    [
                        "validate-artifact",
                        "completion-template",
                        "validate-completion",
                        "kanban_complete",
                    ]
                    if resume_mode == "protocol-finalization"
                    else []
                ),
            },
        }

    @staticmethod
    def _resume_mode(
        *,
        stage: Stage,
        retry: bool,
        workspace_state: dict,
        remote_head: str | None,
        expected_head: str,
    ) -> str:
        if not retry:
            return "fresh"
        if stage in {Stage.SPEC_REVIEW, Stage.PLAN_REVIEW, Stage.TASKS_REVIEW}:
            return "review-resume"
        if (
            stage in {Stage.SPEC_WRITE, Stage.PLAN_WRITE, Stage.TASKS_WRITE}
            and workspace_state.get("ok") is True
            and workspace_state.get("clean") is True
            and workspace_state.get("head_sha")
            and workspace_state.get("head_sha") == remote_head
            and workspace_state.get("head_sha") != expected_head
        ):
            return "protocol-finalization"
        return "artifact-repair"

    def completion_template(self, raw: dict) -> dict:
        card_id = str(raw.get("card_id") or "")
        try:
            outcome = Outcome(str(raw.get("outcome") or ""))
        except ValueError as exc:
            raise ValueError("outcome must be pass, fail, or cancelled") from exc
        managed, item, run = self._managed_work(card_id)
        record = parse_card_body(item.task.body)
        stage = Stage(managed.stage)
        document_stages = {
            Stage.SPEC_WRITE,
            Stage.SPEC_REVIEW,
            Stage.PLAN_WRITE,
            Stage.PLAN_REVIEW,
            Stage.TASKS_WRITE,
            Stage.TASKS_REVIEW,
        }
        if record.mode == WorkMode.FINALIZATION and outcome == Outcome.PASS:
            raise ValueError(
                "completion_template_requires_forced_advance_evidence"
            )
        binding = self.store.delivery_binding(run.run_key)
        mr = self._delivery_mr(run)
        current_head = (
            str(mr.get("sha") or "")
            if mr is not None
            else record.expected_head_sha
        )
        data: dict = {
            "protocol_version": "hollysys-controller/v4",
            "run_key": run.run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "context_digest": record.context_digest,
            "stage": stage.value,
            "iteration": managed.iteration,
            "mode": record.mode.value,
            "outcome": outcome.value,
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
            "kanban_card_id": card_id,
            "head_before_sha": record.expected_head_sha,
            "deterministic_checks": [],
            "verification": [],
            "issues": (
                ["REPLACE_WITH_CONCRETE_FINDING"]
                if outcome == Outcome.FAIL
                else []
            ),
        }
        if binding is not None and mr is not None:
            data.update(
                {
                    "mr_iid": binding.mr_iid,
                    "mr_url": str(binding.mr_url),
                    "head_sha": current_head,
                }
            )
        if stage in document_stages and outcome in {
            Outcome.PASS,
            Outcome.FAIL,
        }:
            validation = self.validate_artifact({"card_id": card_id})
            paths = list(validation["artifact_paths"])
            artifact_digest = self.gitlab.artifact_digest(
                run.project.project_id,
                str(validation["head_sha"]),
                paths,
            )
            data.update(
                {
                    "artifact_commit_sha": validation["head_sha"],
                    "artifact_digest": artifact_digest,
                    "artifact_paths": paths,
                    "deterministic_checks": [
                        {
                            key: validation[key]
                            for key in (
                                "validator",
                                "validator_version",
                                "input_digest",
                                "passed",
                                "error_codes",
                                "result_digest",
                            )
                        }
                    ],
                }
            )
        if (
            stage
            in {
                Stage.SPEC_WRITE,
                Stage.PLAN_WRITE,
                Stage.TASKS_WRITE,
                Stage.IMPLEMENT,
            }
            and outcome == Outcome.PASS
        ):
            data["repository_evidence"] = {
                "repository_base_sha": run.workspace.repository_base_sha,
                "inspected_paths": [run.source.prd_path],
                "existing_capabilities": [
                    "REPLACE_WITH_INSPECTED_EXISTING_CAPABILITY"
                ],
                "change_strategy": "extend_existing",
                "reuse_decisions": [
                    "REPLACE_WITH_CONCRETE_REUSE_DECISION"
                ],
            }
        if stage in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
        } and outcome == Outcome.PASS:
            data["baseline_disposition"] = "reviewed"
        if stage == Stage.TEST and outcome in {Outcome.PASS, Outcome.FAIL}:
            data["test_disposition"] = "executed"
        if (
            stage in {Stage.TASKS_REVIEW, Stage.CODE_REVIEW}
            and outcome == Outcome.PASS
        ):
            baseline = next(
                (
                    candidate
                    for candidate in reversed(record.frozen_baselines)
                    if candidate.phase == "tasks"
                ),
                None,
            )
            gate_paths = (
                baseline.artifact_paths
                if baseline is not None
                else list(data.get("artifact_paths") or [])
            )
            gate_commit = (
                baseline.artifact_commit_sha
                if baseline is not None
                else data.get("artifact_commit_sha")
            )
            gate_digest = (
                baseline.artifact_digest
                if baseline is not None
                else data.get("artifact_digest")
            )
            data.update(
                {
                    "gate_phase": (
                        "implementation_entry"
                        if stage == Stage.TASKS_REVIEW
                        else "implementation_completion"
                    ),
                    "gate_decision": "approved",
                    "gate_reviewer": "id:1",
                    "gate_reviewed_at": run.started_at.isoformat(),
                    "gate_reason": "REPLACE_WITH_CONCRETE_GATE_REASON",
                    "gate_evidence_refs": [
                        f"{binding.mr_url}#note_1"
                        if binding is not None
                        else "evidence/missing-binding"
                    ],
                    "gate_artifact_paths": gate_paths,
                    "gate_artifact_commit_sha": gate_commit,
                    "gate_artifact_digest": gate_digest,
                    "contract_refs": ["REPLACE_WITH_CONTRACT_REF"],
                    "requirement_ids": ["REPLACE_WITH_REQUIREMENT_ID"],
                }
            )
        return CompletionMetadata.model_validate(data).model_dump(mode="json")

    def validate_artifact(self, raw: dict) -> dict:
        started = time.monotonic()
        card_id = str(raw.get("card_id") or "")
        managed, _item, run = self._managed_work(card_id)
        stage = Stage(managed.stage)
        pattern_stage = {
            Stage.SPEC_WRITE: Stage.SPEC_REVIEW,
            Stage.SPEC_REVIEW: Stage.SPEC_REVIEW,
            Stage.PLAN_WRITE: Stage.PLAN_REVIEW,
            Stage.PLAN_REVIEW: Stage.PLAN_REVIEW,
            Stage.TASKS_WRITE: Stage.TASKS_REVIEW,
            Stage.TASKS_REVIEW: Stage.TASKS_REVIEW,
        }.get(stage)
        if pattern_stage is None:
            raise ValueError(f"stage_has_no_artifact_validator:{stage.value}")
        mr = self._delivery_mr(run)
        if mr is None or not mr.get("sha"):
            raise ValueError("artifact validation requires a bound delivery MR")
        head_sha = str(mr["sha"])
        fetch_started = time.monotonic()
        patterns = self.config.artifact_patterns.get(pattern_stage.value, [])
        try:
            paths = self.gitlab.artifact_paths(
                run.project.project_id,
                head_sha,
                patterns,
            )
        except ValueError as exc:
            if "matched no files" not in str(exc):
                raise
            raise ValueError(
                "remote_artifact_missing: validate-artifact reads the bound "
                "GitLab MR head only; commit and push the artifact, verify the "
                f"remote head, then retry; stage={stage.value}; "
                f"head={head_sha}; patterns={patterns}"
            ) from exc
        documents: list[str] = []
        for path in paths:
            payload = self.gitlab.file(run.project.project_id, path, head_sha)
            encoding = str(payload.get("encoding") or "")
            content = str(payload.get("content") or "")
            if encoding != "base64":
                raise ValueError("artifact_content_encoding_unsupported")
            try:
                documents.append(
                    base64.b64decode(content, validate=True).decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("artifact_content_invalid_utf8") from exc
        fetch_ms = int((time.monotonic() - fetch_started) * 1000)
        validator_started = time.monotonic()
        if pattern_stage == Stage.TASKS_REVIEW:
            validation = validate_task_documents(documents).as_dict()
        else:
            input_digest = hashlib.sha256(
                json.dumps(
                    documents,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            result_digest = hashlib.sha256(
                f"artifact-set/v1:{input_digest}:pass".encode()
            ).hexdigest()
            validation = {
                "validator": "artifact-set",
                "validator_version": "artifact-set/v1",
                "input_digest": input_digest,
                "passed": True,
                "error_codes": [],
                "result_digest": result_digest,
            }
        validator_ms = int((time.monotonic() - validator_started) * 1000)
        total_ms = int((time.monotonic() - started) * 1000)
        timing = {
            "git_artifact_ms": fetch_ms,
            "deterministic_check_ms": validator_ms,
            "total_ms": total_ms,
        }
        validation_id = self.store.record_validation(
            run_key=run.run_key,
            card_id=card_id,
            validator=str(validation["validator"]),
            validator_version=str(validation["validator_version"]),
            input_digest=str(validation["input_digest"]),
            result_digest=str(validation["result_digest"]),
            passed=bool(validation["passed"]),
            error_codes=list(validation["error_codes"]),
            timing=timing,
        )
        return {
            "ok": bool(validation["passed"]),
            "validation_id": validation_id,
            "run_key": run.run_key,
            "card_id": card_id,
            "head_sha": head_sha,
            "source": "bound_delivery_remote_head",
            "required_order": [
                "commit",
                "push",
                "verify_remote_head",
                "validate-artifact",
            ],
            "artifact_paths": paths,
            **validation,
            "timing": timing,
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
                        "human block is not an allowed v4 technical/safety block"
                    )
                stage = Stage(managed.stage)
                original_record = parse_card_body(task.body)
                control = self._run_control(run.run_key)
                if control["state"] == "human_blocked":
                    self.store.transition_run(
                        run.run_key,
                        expected_states={"human_blocked"},
                        new_state="active",
                        reason=(
                            f"human_resolve:{request.block_id}:"
                            "actor=hollysys-controller"
                        ),
                        expected_version=int(control["state_version"]),
                    )
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
                    markdown_payload(
                        run.origin,
                        self._render_notification(
                            run,
                            icon="✅",
                            title="已记录并恢复自动交付",
                            fields=[
                                ("任务 ID", inline_code(run.run_key)),
                                ("阶段", format_stage(stage)),
                                ("已解决 Card", inline_code(task.id)),
                                ("新 Card", inline_code(retry.id)),
                            ],
                        ),
                    ),
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
            try:
                history, run = self._history(request.run_key)
                initialization_pending = False
            except ValueError as exc:
                if str(exc) != f"unknown run {request.run_key}":
                    raise
                run = self.store.run_record(request.run_key)
                if run is None:
                    raise
                history = []
                initialization_pending = True
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
            resumed_initialization = None
            if initialization_pending:
                try:
                    resumed_initialization = self._initialize_run(
                        run,
                        run.workspace.repository_base_sha,
                    )
                except Exception as exc:
                    self.store.set_run_exception(
                        request.run_key,
                        self._error_text(exc),
                    )
                    self._enqueue_controller_failure(request.run_key, exc)
                    raise
            self.store.enqueue(
                (
                    f"{request.run_key}:exception-recovered:"
                    f"{recovered['state_version']}"
                ),
                request.run_key,
                "exception-recovered",
                markdown_payload(
                    run.origin,
                    self._render_notification(
                        run,
                        icon="✅",
                        title="已授权从异常状态恢复自动交付",
                        fields=[
                            ("任务 ID", inline_code(request.run_key)),
                            ("授权人", inline_code(request.sender)),
                            (
                                "恢复原因",
                                escape_markdown(request.reason, limit=500),
                            ),
                        ],
                    ),
                ),
            )
            response = {
                "run_key": request.run_key,
                "state": recovered["state"],
                "state_version": recovered["state_version"],
                "continuation": (
                    "initialization-resumed"
                    if resumed_initialization is not None
                    else "pending-reconcile"
                ),
            }
            if resumed_initialization is not None:
                response.update(
                    {
                        "stage": resumed_initialization["stage"],
                        "active_card": resumed_initialization["active_card"],
                    }
                )
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
            pending_reconcile: dict[str, tuple[int, str]] = {}
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
                    pending_reconcile[managed.run_key] = (
                        event.id,
                        f"kanban:{event.kind}",
                    )
            for run_key, (event_id, reason) in pending_reconcile.items():
                self.store.enqueue_reconcile(
                    run_key,
                    reason=reason,
                    event_id=event_id,
                )
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
            for run_key in self.store.run_keys():
                try:
                    self._upgrade_legacy_exhausted_code_exception(run_key)
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                except ControllerFatalError:
                    raise
                except Exception as exc:
                    raise ControllerFatalError(
                        f"legacy_code_terminal_upgrade_failed:{run_key}:{exc}"
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
            for run_key in self.store.active_reconcile_run_keys():
                self.store.enqueue_reconcile(
                    run_key,
                    reason="periodic-full-reconcile",
                )
            self.last_reconcile_at = int(time.time())
            self.last_reconcile_error = None
            self.flush_outbox()
        except Exception as exc:
            self.last_reconcile_error = str(exc)
            raise

    def _upgrade_legacy_exhausted_code_exception(self, run_key: str) -> bool:
        control = self.store.run_control(run_key)
        if control is None or control["state"] != "exception":
            return False
        prefix = (
            "code modification limit "
            f"{self.config.code_modification_limit} exhausted;"
        )
        if not str(control.get("last_transition_reason") or "").startswith(prefix):
            return False
        if not self._begin_reconcile(run_key):
            return False
        try:
            history, run = self._history(run_key)
            if self._code_modification_count(history) < self.config.code_modification_limit:
                return False
            work = [item for item in history if item.managed.purpose == "work"]
            if not work:
                return False
            latest = work[-1]
            if (
                latest.managed.stage != Stage.CODE_REVIEW.value
                or latest.task.status != "done"
            ):
                return False
            review = validate_persisted_completion_metadata(
                latest.task.latest_metadata
            )
            test = self._latest_valid_completion(
                history,
                Stage.TEST,
                {Outcome.PASS},
            )
            if (
                review.outcome != Outcome.FAIL
                or test is None
                or test.mr_iid != review.mr_iid
                or test.mr_url != review.mr_url
                or test.head_sha != review.head_sha
            ):
                return False
            active_work = [
                item
                for item in history
                if item.managed.purpose == "work"
                and item.task.status in ACTIVE_STATUSES
            ]
            if active_work:
                return False
            for item in history:
                if (
                    item.managed.purpose == "exception"
                    and item.task.status in ACTIVE_STATUSES
                ):
                    self.kanban.comment(
                        run.workspace.board,
                        item.task.id,
                        "[controller-terminal-upgrade:v1]\n"
                        "outcome: completed_with_findings\n"
                        f"head_sha: {review.head_sha}\n"
                        "reason: legacy exhausted CODE exception is now a "
                        "non-merge terminal outcome",
                        "hollysys-controller",
                    )
                    self.kanban.abort_task(
                        run.workspace.board,
                        item.task.id,
                        "preserved legacy exception as completed_with_findings",
                    )
            self.store.transition_run(
                run_key,
                expected_states={"exception"},
                new_state="active",
                reason="upgrade_legacy_exhausted_code_exception",
                expected_version=int(control["state_version"]),
            )
            self.store.enqueue_reconcile(
                run_key,
                reason="upgrade-legacy-exhausted-code-exception",
            )
            return True
        finally:
            self._finish_reconcile(run_key)

    def consume_reconcile_once(self, lease_owner: str) -> bool:
        intent = self.store.claim_reconcile(
            lease_owner=lease_owner,
            lease_seconds=max(60, self.config.command_timeout_seconds * 3),
        )
        if intent is None:
            return False
        intent_id = str(intent["intent_id"])
        run_key = str(intent["run_key"])
        started = time.monotonic()
        try:
            self.store.update_reconcile_step(
                intent_id,
                lease_owner=lease_owner,
                step="recompute-kanban-gitlab",
                lease_seconds=max(60, self.config.command_timeout_seconds * 3),
            )
            self._reconcile_run_with_policy(run_key)
            duration_ms = int((time.monotonic() - started) * 1000)
            self.store.update_reconcile_step(
                intent_id,
                lease_owner=lease_owner,
                step=f"completed:{duration_ms}ms",
                lease_seconds=60,
            )
            self.store.finish_reconcile(
                intent_id,
                lease_owner=lease_owner,
            )
            self.last_reconcile_at = int(time.time())
            self.last_reconcile_error = None
            return True
        except Exception as exc:
            self.store.finish_reconcile(
                intent_id,
                lease_owner=lease_owner,
                error=self._error_text(exc),
            )
            self.last_reconcile_error = self._error_text(exc)
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

    def _promotion_is_authorized(self, item: HistoryItem) -> bool:
        """Bind a manual-looking promotion to Controller-owned evidence.

        Hermes names every ``kanban promote`` event ``promoted_manual``, even
        when Controller publishes a newly created, initially blocked card.
        A single promotion is therefore legitimate when the matching durable
        release operation is in flight or completed. Any additional promotion
        still requires the audited human-resolution comment.
        """
        if any(
            "[human-resolution:v1]" in str(comment["body"])
            and "resolved_by:" in str(comment["body"])
            and "new_card_id:" in str(comment["body"])
            for comment in item.task.comments
        ):
            return True
        if item.task.event_kinds.count("promoted_manual") != 1:
            return False
        operation = self.store.operation_record(
            f"{item.managed.idempotency_key}:release"
        )
        if (
            operation is None
            or operation.get("kind") != "release"
            or operation.get("status")
            not in {"executing", "uncertain", "done"}
        ):
            return False
        try:
            payload = json.loads(str(operation.get("payload") or ""))
        except json.JSONDecodeError:
            return False
        return payload == {
            "board": item.managed.board,
            "card_id": item.task.id,
        }

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
        if control and control["state"] == "human_blocked":
            return
        if self._dependency_retry_blocked("gitlab"):
            return
        history, run = self._history(run_key)
        unauthorized_promotion = next(
            (
                item
                for item in history
                if "promoted_manual" in item.task.event_kinds
                and not self._promotion_is_authorized(item)
            ),
            None,
        )
        if unauthorized_promotion is not None:
            self._exception(
                run,
                unauthorized_promotion.task.id,
                "unknown promoted_manual event without matching human resolve",
            )
            return
        mr = self._delivery_mr(run)
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
                    self._ensure_work_published(run, item.task)
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
                            "[controller-block-rejected:v4]"
                            in str(comment["body"])
                            for comment in item.task.comments
                        ):
                            self._assert_reconcile_mutable(run_key)
                            self.kanban.comment(
                                run.workspace.board,
                                item.task.id,
                                "[controller-block-rejected:v4]\n"
                                f"reason: {reason}",
                                "hollysys-controller",
                            )
                        self._exception(
                            run,
                            item.task.id,
                            reason,
                        )
                        return
                    self._enqueue_human_block(run, item, human_block_comment)
                    current = self._run_control(run_key)
                    if current["state"] != "human_blocked":
                        self.store.transition_run(
                            run_key,
                            expected_states={
                                "active",
                                "dependency_degraded",
                                "merge_wait",
                                "transition_pending",
                                "retry_wait",
                            },
                            new_state="human_blocked",
                            reason=f"human_block:{item.task.id}",
                            expected_version=int(current["state_version"]),
                        )
                    return
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
                    self._exception(
                        run,
                        item.task.id,
                        "blocked card has no valid human block contract",
                    )
                    return
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
                        history=history,
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
                    history=history,
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
                        history=history,
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
                        history=history,
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
        self._enqueue_agent_completed(run, latest, metadata, history=history)

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
        if route.terminal_state:
            self._finalize_code_flow(
                run,
                history,
                latest,
                metadata,
                terminal_state=route.terminal_state,
                paired_test=paired_test,
                code_modifications=code_modifications,
            )
            return
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
                metadata.stage == Stage.TEST
                and metadata.outcome == Outcome.FAIL
            ):
                next_modification = code_modifications + 1
                repair_context = RepairContext(
                    kind=RepairKind.CODE_GATE_FAILURE,
                    trigger_card_id=latest.task.id,
                    related_card_ids=[metadata.kanban_card_id],
                    head_sha=metadata.head_sha,
                    code_modification=next_modification,
                    code_modification_limit=self.config.code_modification_limit,
                    issues=self._test_gate_issues(metadata),
                )
                self._enqueue_code_retry(
                    run,
                    metadata,
                    None,
                    next_modification,
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
                    review_attempts.get(metadata.stage, 0),
                )
            if metadata.mode == WorkMode.FINALIZATION:
                final_review_stage = DOCUMENT_REVIEW_FOR_PRODUCER[
                    metadata.stage
                ]
                self._enqueue_phase_frozen(
                    run,
                    PHASE_FOR_STAGE[metadata.stage],
                    metadata,
                    review_attempts.get(final_review_stage, 0),
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
            if live_mr.get("draft") or live_mr.get("work_in_progress"):
                binding = self.store.delivery_binding(run.run_key)
                if binding is None:
                    raise ValueError("merge-ready delivery has no binding")
                self._mark_delivery_ready_at_head(
                    run,
                    binding,
                    current_head,
                )
                live_mr = self.gitlab.delivery_mr(run, test.mr_iid)
                if (
                    live_mr is None
                    or str(live_mr.get("sha") or "") != current_head
                ):
                    self.store.clear_merge_wait(run.run_key)
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

    def _finalize_code_flow(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        latest: HistoryItem,
        metadata: CompletionMetadata,
        *,
        terminal_state: str,
        paired_test: CompletionMetadata | None,
        code_modifications: int,
    ) -> None:
        if terminal_state == "completed_test_failed":
            if metadata.stage != Stage.TEST or metadata.outcome != Outcome.FAIL:
                raise ValueError("test-failed terminal requires a failed test")
            test = metadata
            review = None
        else:
            if (
                metadata.stage != Stage.CODE_REVIEW
                or paired_test is None
                or paired_test.outcome != Outcome.PASS
            ):
                raise ValueError("ready terminal requires a passed paired test")
            test = paired_test
            review = metadata

        head = str(metadata.head_sha or test.head_sha or "")
        mr_iid = metadata.mr_iid or test.mr_iid
        if mr_iid is None or not head:
            raise ValueError("CODE terminal requires MR/head evidence")
        live_mr = self.gitlab.delivery_mr(run, int(mr_iid))
        if live_mr is None or str(live_mr.get("sha") or "") != head:
            self._create_work(run, Stage.TEST, latest.task.id)
            return
        violation = self._frozen_violation(run, history, head)
        if violation is not None:
            self._exception(
                run,
                latest.task.id,
                f"terminal frozen baseline violation: {violation}",
            )
            return

        ready_required = terminal_state in {
            "completed_ready",
            "completed_with_findings",
        }
        if ready_required:
            binding = self.store.delivery_binding(run.run_key)
            if binding is None or binding.mr_iid != int(mr_iid):
                raise ValueError("CODE terminal MR does not match delivery binding")
            self._mark_delivery_ready_at_head(run, binding, head)
            live_mr = self.gitlab.delivery_mr(run, int(mr_iid))
            if (
                live_mr is None
                or str(live_mr.get("sha") or "") != head
                or live_mr.get("draft")
                or live_mr.get("work_in_progress")
            ):
                raise CheckedHeadConflict(
                    "delivery-ready verification changed before terminal commit"
                )

        self.store.clear_merge_wait(run.run_key)
        reason = (
            f"code_flow_terminal:{terminal_state};head={head};"
            f"tester={test.outcome.value};"
            f"code-reviewer={review.outcome.value if review else 'not_run'};"
            f"modifications={code_modifications}/"
            f"{self.config.code_modification_limit};mr_ready={ready_required}"
        )
        self.store.mark_flow_completed(
            run.run_key,
            state=terminal_state,
            checked_head=head,
            reason=reason,
        )
        self._enqueue_code_flow_completed(
            run,
            history,
            terminal_state=terminal_state,
            test=test,
            review=review,
            mr=live_mr,
            code_modifications=code_modifications,
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
                self._render_notification(
                    run,
                    icon="⚠️",
                    title="代码门禁已完成，正在等待合并条件",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage("merge-wait")),
                        (
                            "阻塞原因",
                            escape_markdown(blocker.kind, limit=100),
                        ),
                        ("Head", inline_code(short_sha(current_head))),
                        (
                            "责任人",
                            escape_markdown(
                                blocker.owner or "unknown",
                                limit=100,
                            ),
                        ),
                        (
                            "MR",
                            markdown_link(
                                f"!{live_mr.get('iid') or 'MR'}",
                                live_mr.get("web_url") or blocker.url or "",
                            ),
                        ),
                        (
                            "相关链接",
                            markdown_link(
                                "查看阻塞详情",
                                blocker.url or live_mr.get("web_url") or "",
                            ),
                        ),
                        (
                            "下次重试时间",
                            inline_code(waiting["next_retry_at"]),
                        ),
                    ],
                ),
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
                if "content" in payload:
                    content = str(payload["content"])
                    message_format = str(payload.get("format") or "markdown")
                else:
                    # Preserve historical outbox payloads exactly when a
                    # controller restart replays them.
                    content = str(payload["text"])
                    message_format = "text"
                if message_format not in {"text", "markdown"}:
                    raise ValueError(
                        f"unsupported outbox message format:{message_format}"
                    )
                self.notifier.send(
                    item["outbox_key"],
                    origin,
                    content,
                    message_format=message_format,
                )
                self.store.finish_outbox(item["outbox_key"])
            except (
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
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
        binding = self.store.delivery_binding(run_key)
        mr = self.gitlab.abort_delivery(
            run,
            mr_iid=(binding.mr_iid if binding is not None else None),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="⛔",
                    title=(
                        "废止请求到达前交付已完成合并"
                        if terminal == "completed_before_abort"
                        else "自动交付已按人类确认废止"
                    ),
                    fields=[
                        ("任务 ID", inline_code(run_key)),
                        (
                            "申请人",
                            inline_code(
                                control.get("abort_requested_by") or "unknown"
                            ),
                        ),
                        ("原因", escape_markdown(reason, limit=500)),
                        (
                            "MR",
                            markdown_link(
                                f"!{mr.get('iid') or 'MR'}",
                                mr.get("web_url") or "",
                            ),
                        ),
                        (
                            "MR 状态",
                            escape_markdown(mr.get("state") or "unknown"),
                        ),
                        ("分支与 Worktree", "已保留"),
                    ],
                ),
            ),
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
        if worker_session_id is None and event.run_id is not None:
            # Hermes v2026.7.30 identifies attempts by task_events.run_id but
            # does not emit worker_session_id in claimed/heartbeat/terminal
            # payloads.  Preserve that durable identity so health timelines
            # do not collapse multiple retries into one perpetually-running
            # attempt.
            worker_session_id = f"kanban-run:{event.run_id}"
        if hasattr(self.store, "record_card_runtime_event"):
            self.store.record_card_runtime_event(
                board=managed.board,
                card_id=managed.card_id,
                kind=event.kind,
                created_at=event.created_at,
                run_id=(
                    f"{managed.board}:{event.run_id}"
                    if event.run_id is not None
                    else None
                ),
                worker_session_id=worker_session_id,
                worker_pid=worker_pid,
                lease_seconds=self.config.worker_progress_lease_seconds,
            )
        started = event.kind in {"claimed", "started", "worker_started"}
        interrupted = event.kind in {
            "blocked",
            "crashed",
            "rate_limited",
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
            history, run = self._history(managed.run_key)
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
        round_field = self._round_field(
            document_round=self._document_round(
                history,
                Stage(managed.stage),
                accepted_completion=False,
            ),
            worker_attempt=attempt,
        )
        state_version = int(
            self._run_control(run.run_key).get("state_version") or 1
        )
        if started:
            agent_label = format_agent(task.assignee)
            self._enqueue_progress(
                run,
                (
                    f"agent:{managed.card_id}:{attempt}:"
                    f"started:{state_version}"
                ),
                self._render_notification(
                    run,
                    icon="ℹ️",
                    title=f"{agent_label} Agent 已开始工作",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage(managed.stage)),
                        round_field,
                        ("Agent", agent_label),
                        ("Card", inline_code(task.id)),
                    ],
                ),
            )
        elif interrupted:
            agent_label = format_agent(task.assignee)
            self._enqueue_progress(
                run,
                (
                    f"agent:{managed.card_id}:{attempt}:"
                    f"interrupted-{event.kind}:"
                    f"{state_version}"
                ),
                self._render_notification(
                    run,
                    icon="⚠️",
                    title=f"{agent_label} Agent 工作已中断",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage(managed.stage)),
                        round_field,
                        ("Agent", agent_label),
                        ("Card", inline_code(task.id)),
                        ("事件", format_event(event.kind)),
                    ],
                ),
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
                    history, run = self._history(run_key)
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
                document_round = self._document_round(
                    history,
                    Stage(str(runtime["stage"])),
                    accepted_completion=False,
                )
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
                        document_round=document_round,
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
                        document_round=document_round,
                    )
                    continue
                if not hasattr(self.gitlab, "local_workspace_state"):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "workspace_probe_unavailable",
                        "local workspace probe is unavailable",
                        document_round=document_round,
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
                        document_round=document_round,
                    )
                    continue
                if not hasattr(self.gitlab, "delivery_mr"):
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        "mr_probe_unavailable",
                        "delivery MR probe is unavailable",
                        document_round=document_round,
                    )
                    continue
                try:
                    binding = self.store.delivery_binding(run.run_key)
                    if runtime.get("mr_iid") and (
                        binding is None
                        or binding.mr_iid != int(runtime["mr_iid"])
                    ):
                        raise ValueError(
                            "runtime MR is not the persisted delivery binding"
                        )
                    mr = self._delivery_mr(run)
                except DependencyError as exc:
                    self._handle_dependency_error(run_key, exc)
                    self._enqueue_worker_lease_notice(
                        run,
                        runtime,
                        deadline,
                        exc.context.error_code or exc.error_class,
                        "MR/head evidence is temporarily unavailable",
                        document_round=document_round,
                    )
                    break
                persisted_mr = runtime.get("mr_iid")
                persisted_head = str(runtime.get("head_sha") or "")
                if mr is None:
                    mr_evidence_matches = bool(
                        runtime.get("stage") == Stage.SPEC_WRITE.value
                        and int(runtime.get("iteration") or 0) == 1
                        and not persisted_mr
                        and not persisted_head
                    )
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
                        document_round=document_round,
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
                            self._render_notification(
                                run,
                                icon="⚠️",
                                title="Agent 失联，已请求有限重派",
                                fields=[
                                    ("任务 ID", inline_code(run_key)),
                                    (
                                        "阶段",
                                        format_stage(runtime["stage"]),
                                    ),
                                    self._round_field(
                                        document_round=document_round,
                                        worker_attempt=int(
                                            runtime.get("attempt") or 1
                                        ),
                                    ),
                                    (
                                        "Card",
                                        inline_code(runtime["card_id"]),
                                    ),
                                    (
                                        "重派次数",
                                        (
                                            f"{redispatch_count + 1}/"
                                            f"{self.config.worker_redispatch_limit}"
                                        ),
                                    ),
                                ],
                            ),
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
        *,
        document_round: int | None = None,
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="⚠️",
                    title="Agent 超过进展租约，暂未自动重派",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage(runtime["stage"])),
                        self._round_field(
                            document_round=document_round,
                            worker_attempt=int(
                                runtime.get("attempt") or 1
                            ),
                        ),
                        ("Card", inline_code(runtime["card_id"])),
                        (
                            "Session",
                            inline_code(
                                runtime.get("worker_session_id") or "unknown"
                            ),
                        ),
                        (
                            "PID",
                            inline_code(runtime.get("worker_pid") or "unknown"),
                        ),
                        ("错误码", inline_code(error_code)),
                        ("处理", "证据不足，Controller 未终止或重派该 Agent"),
                    ],
                    sections=[
                        (
                            "判断依据",
                            [escape_markdown(explanation, limit=700)],
                        )
                    ],
                ),
            ),
        )

    def _enqueue_agent_completed(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
        *,
        history: list[HistoryItem] | None = None,
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
        document_round = (
            self._document_round(
                history,
                metadata.stage,
                accepted_completion=True,
            )
            if history is not None
            else (
                min(
                    self.config.document_review_limit,
                    max(1, metadata.iteration),
                )
                if metadata.stage
                in {
                    *DOCUMENT_REVIEW_FOR_PRODUCER,
                    *DOCUMENT_REVIEW_FOR_PRODUCER.values(),
                }
                else None
            )
        )
        agent_label = format_agent(item.task.assignee)
        related_links = list(
            dict.fromkeys(
                [
                    *([str(metadata.mr_url)] if metadata.mr_url else []),
                    *(str(url) for url in metadata.gitlab_urls),
                    *(str(url) for url in metadata.gate_evidence_refs),
                ]
            )
        )
        self._enqueue_progress(
            run,
            (
                f"agent:{item.task.id}:{attempt}:"
                f"completed-accepted:{metadata.outcome.value}:"
                f"{self._run_control(run.run_key).get('state_version', 1)}"
            ),
            self._render_notification(
                run,
                icon="✅",
                title=f"{agent_label} Agent 工作已完成",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("阶段", format_stage(metadata.stage)),
                    self._round_field(
                        document_round=document_round,
                        worker_attempt=attempt,
                    ),
                    ("Agent", agent_label),
                    ("Card", inline_code(item.task.id)),
                    ("结论", format_outcome(metadata.outcome)),
                    ("耗时", format_duration(duration)),
                ],
                sections=[
                    (
                        "相关链接",
                        [
                            markdown_link(gitlab_link_label(url), url)
                            for url in related_links
                        ],
                    )
                ],
            ),
        )

    def _enqueue_agent_completion_rejected(
        self,
        run: RunRecord,
        item: HistoryItem,
        reason: str,
        *,
        history: list[HistoryItem] | None = None,
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
        stage = Stage(item.managed.stage)
        document_round = (
            self._document_round(
                history,
                stage,
                accepted_completion=False,
            )
            if history is not None
            else (
                min(
                    self.config.document_review_limit,
                    max(1, item.managed.iteration),
                )
                if stage
                in {
                    *DOCUMENT_REVIEW_FOR_PRODUCER,
                    *DOCUMENT_REVIEW_FOR_PRODUCER.values(),
                }
                else None
            )
        )
        agent_label = format_agent(item.task.assignee)
        self._enqueue_progress(
            run,
            (
                f"agent:{item.task.id}:{attempt}:"
                f"completed-rejected:"
                f"{self._run_control(run.run_key).get('state_version', 1)}"
            ),
            self._render_notification(
                run,
                icon="⚠️",
                title=f"{agent_label} Agent 完成结果未被接受",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("阶段", format_stage(item.managed.stage)),
                    self._round_field(
                        document_round=document_round,
                        worker_attempt=attempt,
                    ),
                    ("Agent", agent_label),
                    ("Card", inline_code(item.task.id)),
                    ("结论", format_outcome("rejected")),
                ],
                sections=[
                    (
                        "拒绝原因",
                        [human_summary(reason, limit=180)],
                    )
                ],
            ),
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
        mr = self._delivery_mr(run)
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
                    self._render_notification(
                        run,
                        icon="⚠️",
                        title="发现冻结工件被修改，已改派恢复任务",
                        fields=[
                            ("任务 ID", inline_code(run.run_key)),
                            (
                                "恢复阶段",
                                escape_markdown(phase.value.upper()),
                            ),
                        ],
                        sections=[
                            (
                                "发现的问题",
                                [escape_markdown(violation, limit=500)],
                            )
                        ],
                    ),
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
        accepted_preflight = self.store.deployment_preflight(
            include_digest=True
        )
        if (
            accepted_preflight is not None
            and accepted_preflight.get("ok")
            and accepted_preflight.get("deep")
        ):
            admission = profile_preflight(
                self.config,
                assignee,
                deep=True,
                project_path=run.project.project_path,
                branch=run.workspace.branch,
            )
            self.store.record_profile_preflight(admission, deep=True)
            if not admission.get("ok"):
                error_code = str(
                    admission.get("error_code")
                    or "profile_admission_failed"
                )
                control = self._run_control(run.run_key)
                if control["state"] in {
                    "active",
                    "transition_pending",
                    "dependency_degraded",
                    "merge_wait",
                    "retry_wait",
                }:
                    transient = error_code in {
                        "dependency_unavailable",
                        "rate_limited",
                    }
                    self.store.transition_run(
                        run.run_key,
                        expected_states={str(control["state"])},
                        new_state=(
                            "retry_wait" if transient else "human_blocked"
                        ),
                        reason=(
                            f"profile_admission:{assignee}:{error_code}"
                        ),
                        expected_version=int(control["state_version"]),
                        next_retry_at=(
                            int(time.time())
                            + int(
                                self.config.dependency_backoff_initial_seconds
                            )
                            if transient
                            else None
                        ),
                    )
                raise RunPolicyError(
                    f"profile_admission_failed:{assignee}:{error_code}"
                )
        binding = self.store.delivery_binding(run.run_key)
        expected_head_sha = (
            str(mr.get("sha"))
            if mr is not None and mr.get("sha")
            else run.workspace.repository_base_sha
        )
        context_payload = {
            "protocol_version": "hollysys-controller/v4",
            "run_key": run.run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "stage": stage.value,
            "iteration": iteration,
            "mode": mode.value,
            "idempotency_key": key,
            "parent_card_id": parent_card_id,
            "assignee": assignee,
            "skills": skills,
            "expected_head_sha": expected_head_sha,
            "delivery": (
                binding.model_dump(mode="json") if binding is not None else None
            ),
            "frozen_baselines": [
                baseline.model_dump(mode="json")
                for baseline in frozen_baselines
            ],
            "repair_context": (
                repair_context.model_dump(mode="json")
                if repair_context is not None
                else None
            ),
            "resume_answer": resume_answer,
            "resumed_from_card_id": resumed_from,
        }
        context_digest = hashlib.sha256(
            json.dumps(
                context_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
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
            expected_head_sha=expected_head_sha,
            context_digest=context_digest,
            scratch_dir=(
                f"/opt/data/scratch/{run.run_generation}/"
                f"{hashlib.sha256(key.encode()).hexdigest()[:20]}"
            ),
            delivery=binding,
        )
        self._prepare_card_scratch_dir(record)
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

    def _delivery_mr(self, run: RunRecord) -> dict | None:
        binding = self.store.delivery_binding(run.run_key)
        if binding is None:
            return None
        return self.gitlab.validate_delivery_binding(run, binding)

    def _attempts_by_stage(self, history: list[HistoryItem]) -> dict[Stage, int]:
        result: dict[Stage, int] = {}
        for item in history:
            if item.managed.purpose != "work":
                continue
            stage = Stage(item.managed.stage)
            if any(
                "[controller-protocol-error:v4]" in str(comment["body"])
                for comment in item.task.comments
            ):
                continue
            result[stage] = result.get(stage, 0) + 1
        return result

    def _document_round(
        self,
        history: list[HistoryItem],
        stage: Stage,
        *,
        accepted_completion: bool,
    ) -> int | None:
        review_stage = DOCUMENT_REVIEW_FOR_PRODUCER.get(stage)
        is_review = False
        if review_stage is None and stage in set(
            DOCUMENT_REVIEW_FOR_PRODUCER.values()
        ):
            review_stage = stage
            is_review = True
        if review_stage is None:
            return None
        completed_reviews = self._review_attempts_by_stage(history).get(
            review_stage,
            0,
        )
        used = (
            completed_reviews
            if is_review and accepted_completion
            else completed_reviews + 1
        )
        return min(
            self.config.document_review_limit,
            max(1, used),
        )

    def _round_field(
        self,
        *,
        document_round: int | None,
        worker_attempt: int,
    ) -> tuple[str, str]:
        if document_round is not None:
            return (
                "阶段轮次",
                f"{document_round}/{self.config.document_review_limit}",
            )
        return (
            "执行尝试",
            format_attempt(
                worker_attempt,
                self.config.worker_redispatch_limit,
            ),
        )

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
                    "[controller-protocol-error:v4]" in str(comment["body"])
                    for comment in item.task.comments
                )
            ):
                continue
            try:
                metadata = validate_persisted_completion_metadata(
                    item.task.latest_metadata
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
            self._render_notification(
                run,
                icon="⚠️",
                title="检测到冻结工件被修改，已派发恢复任务",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("恢复阶段", escape_markdown(phase.value.upper())),
                ],
                sections=[
                    (
                        "发现的问题",
                        [escape_markdown(violation, limit=500)],
                    )
                ],
            ),
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
                "[controller-protocol-error:v4]" in str(comment["body"])
                for comment in item.task.comments
            ):
                stage = Stage(item.managed.stage)
                result[stage] = result.get(stage, 0) + 1
        return result

    def _validate_completion_identity(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        card = parse_card_body(item.task.body)
        expected = {
            "run_key": run.run_key,
            "source_key": run.source_key,
            "run_generation": run.run_generation,
            "context_digest": card.context_digest,
            "stage": item.managed.stage,
            "iteration": item.managed.iteration,
            "mode": card.mode.value,
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
            "head_before_sha": card.expected_head_sha,
        }
        actual = metadata.model_dump(mode="json")
        mismatches = [
            key for key, value in expected.items() if actual.get(key) != value
        ]
        if mismatches:
            raise ValueError(
                "completion context mismatch: " + ", ".join(sorted(mismatches))
            )
        binding = self.store.delivery_binding(run.run_key)
        if binding is not None:
            if (
                metadata.mr_iid != binding.mr_iid
                or metadata.mr_url is None
                or str(metadata.mr_url) != str(binding.mr_url)
            ):
                raise ValueError(
                    "completion MR does not match persisted delivery binding"
                )
        elif metadata.mr_iid is not None or metadata.mr_url is not None:
            raise ValueError("completion references an unbound delivery MR")
        if metadata.deterministic_checks:
            persisted = self.store.validation_runs(
                run.run_key,
                card_id=item.task.id,
            )
            accepted = {
                (
                    entry["validator"],
                    entry["validator_version"],
                    entry["input_digest"],
                    entry["result_digest"],
                    entry["passed"],
                    tuple(entry["error_codes"]),
                )
                for entry in persisted
            }
            for check in metadata.deterministic_checks:
                candidate = (
                    check.validator,
                    check.validator_version,
                    check.input_digest,
                    check.result_digest,
                    check.passed,
                    tuple(check.error_codes),
                )
                if candidate not in accepted:
                    raise ValueError(
                        "deterministic check was not recomputed by Controller"
                    )
        if (
            metadata.repository_evidence is not None
            and metadata.repository_evidence.repository_base_sha
            != run.workspace.repository_base_sha
        ):
            raise ValueError(
                "repository evidence is not bound to the run base commit"
            )

    def _validate_completion_context(
        self,
        run: RunRecord,
        item: HistoryItem,
        metadata: CompletionMetadata,
    ) -> None:
        self._validate_completion_identity(run, item, metadata)
        binding = self.store.delivery_binding(run.run_key)
        if binding is None:
            return
        mr = self.gitlab.validate_delivery_binding(run, binding)
        if (
            metadata.head_sha is not None
            and metadata.head_sha != str(mr.get("sha") or "")
        ):
            raise ValueError("completion head is not the bound current MR head")

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
        history: list[HistoryItem] | None = None,
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
        self._enqueue_agent_completion_rejected(
            run,
            item,
            reason,
            history=history,
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
        self._assert_reconcile_mutable(run.run_key)
        self._reject_agent_completion(
            run,
            latest,
            reason,
            history=history,
        )
        marker = "[controller-protocol-error:v4]"
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
        parent = self.reader.task(run.workspace.board, parent_card_id)
        if parent is not None and parent.status in ACTIVE_STATUSES:
            self.kanban.abort_task(
                run.workspace.board,
                parent_card_id,
                "Controller entered exception: " + reason[:400],
            )
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="❗",
                    title="自动交付需要异常处理",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage("exception")),
                        ("Agent", format_agent("dispatcher")),
                        ("Card", inline_code(task.id)),
                        (
                            "需要操作",
                            "请查看异常卡与证据，明确决定恢复、调整授权或停止交付",
                        ),
                    ],
                    sections=[
                        (
                            "异常证据",
                            [escape_markdown(reason, limit=700)],
                        )
                    ],
                ),
            ),
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
        managed_item = next(
            (
                item
                for item in self._history(run.run_key)[0]
                if item.task.id == card_id
            ),
            None,
        )
        card = (
            parse_card_body(managed_item.task.body)
            if managed_item is not None
            else None
        )
        if card is None:
            raise ValueError(f"unknown managed card {card_id}")
        return CompletionMetadata(
            protocol_version="hollysys-controller/v4",
            run_key=run.run_key,
            source_key=run.source_key,
            run_generation=run.run_generation,
            context_digest=card.context_digest,
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
            head_before_sha=card.expected_head_sha,
            deterministic_checks=[],
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
            runtime = self.store.card_runtime(
                item.managed.board,
                item.managed.card_id,
            )
            if (
                runtime is None
                or runtime.get("attempt_status") != "completed_accepted"
            ):
                continue
            try:
                metadata = validate_persisted_completion_metadata(
                    item.task.latest_metadata
                )
                self._validate_completion_identity(
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

    @staticmethod
    def _test_gate_issues(test: CompletionMetadata) -> list[str]:
        return [f"[tester] {issue}" for issue in test.issues] or [
            "[tester] 测试未通过，需根据测试证据修改实现。"
        ]

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

    def _mark_delivery_ready_at_head(
        self,
        run: RunRecord,
        binding: DeliveryBinding,
        checked_head: str,
    ) -> dict:
        control = self._run_control(run.run_key)
        state_version = int(control.get("state_version") or 1)

        def mark_and_verify() -> dict:
            before = self.gitlab.delivery_mr(run, binding.mr_iid)
            if before is None or str(before.get("sha") or "") != checked_head:
                raise CheckedHeadConflict(
                    "MR head changed before delivery-ready transition"
                )
            self.gitlab.mark_delivery_ready(run, binding)
            after = self.gitlab.delivery_mr(run, binding.mr_iid)
            if after is None or str(after.get("sha") or "") != checked_head:
                raise CheckedHeadConflict(
                    "MR head changed during delivery-ready transition"
                )
            if after.get("draft") or after.get("work_in_progress"):
                raise DependencyContractError(
                    "GitLab did not confirm delivery MR is ready",
                    context=ErrorContext(
                        dependency="gitlab",
                        endpoint="merge_requests",
                        error_code="delivery_ready_not_confirmed",
                    ),
                )
            return after

        return self._operation(
            f"{run.run_key}:delivery-ready:{checked_head}",
            "delivery-ready",
            {
                "run_key": run.run_key,
                "mr_iid": binding.mr_iid,
                "checked_head": checked_head,
            },
            mark_and_verify,
            run_key=run.run_key,
            expected_state_version=state_version,
            expected_head_sha=checked_head,
        )

    def _render_notification(
        self,
        run: RunRecord,
        *,
        icon: str,
        title: str,
        fields: list[tuple[str, str]],
        sections: list[tuple[str, list[str]]] | None = None,
    ) -> str:
        return render_message(
            mention=self._mention(run.origin),
            icon=icon,
            title=title,
            fields=fields,
            sections=sections or [],
        )

    def _enqueue_progress(
        self,
        run: RunRecord,
        event_key: str,
        content: str,
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
            markdown_payload(run.origin, content),
        )

    def _enqueue_phase_started(
        self, run: RunRecord, phase: Phase, task: TaskRecord
    ) -> None:
        self._enqueue_progress(
            run,
            f"{phase.value}:started",
            self._render_notification(
                run,
                icon="ℹ️",
                title=f"自动交付进入 {phase.value.upper()} 阶段",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("Agent", format_agent(task.assignee)),
                    ("Card", inline_code(task.id)),
                ],
            ),
        )

    def _enqueue_review_failed(
        self,
        run: RunRecord,
        metadata: CompletionMetadata,
        review_attempt: int,
        next_mode: WorkMode,
    ) -> None:
        phase = PHASE_FOR_STAGE[metadata.stage]
        if next_mode == WorkMode.FINALIZATION:
            action = (
                "三轮审查均未通过，已交回本阶段 Writer 完成最终取舍；"
                "完成后将按强制收敛规则冻结。"
            )
        else:
            action = "已退回本阶段 Writer 修订；完成后进入下一轮审查。"
        urls = list(
            dict.fromkeys(
                [
                    *([str(metadata.mr_url)] if metadata.mr_url else []),
                    *(str(url) for url in metadata.gitlab_urls),
                    *(str(url) for url in metadata.gate_evidence_refs),
                ]
            )
        )
        self._enqueue_progress(
            run,
            f"{phase.value}:review-failed:{review_attempt}:{metadata.kanban_card_id}",
            self._render_notification(
                run,
                icon="⚠️",
                title=(
                    f"{phase.value.upper()} 第 "
                    f"{review_attempt}/{self.config.document_review_limit} "
                    "轮审查未通过"
                ),
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    (
                        "审查轮次",
                        f"{review_attempt}/{self.config.document_review_limit}",
                    ),
                    (
                        "下一位 Agent",
                        format_agent(
                            self.config.stage_assignees[
                                PRODUCER_FOR_PHASE[phase]
                            ]
                        ),
                    ),
                    ("后续处理", escape_markdown(action)),
                ],
                sections=[
                    (
                        "主要问题",
                        [
                            human_summary(issue, limit=180)
                            for issue in metadata.issues[:3]
                        ],
                    ),
                    (
                        "相关链接",
                        [
                            markdown_link(
                                gitlab_link_label(
                                    url,
                                    default="查看审查证据",
                                ),
                                url,
                            )
                            for url in urls
                        ],
                    ),
                ],
            ),
        )

    def _enqueue_phase_frozen(
        self,
        run: RunRecord,
        phase: Phase,
        metadata: CompletionMetadata,
        review_attempt: int,
    ) -> None:
        disposition = metadata.baseline_disposition
        label = (
            "审查通过"
            if disposition == BaselineDisposition.REVIEWED
            else "达到审查上限后强制收敛"
        )
        title = (
            f"{phase.value.upper()} 第 "
            f"{review_attempt}/{self.config.document_review_limit} "
            "轮审查通过，工件已冻结"
            if disposition == BaselineDisposition.REVIEWED
            else (
                f"{phase.value.upper()} 已在 "
                f"{review_attempt}/{self.config.document_review_limit} "
                "轮后强制收敛并冻结"
            )
        )
        urls = list(
            dict.fromkeys(
                [
                    *([str(metadata.mr_url)] if metadata.mr_url else []),
                    *(str(url) for url in metadata.gitlab_urls),
                    *(str(url) for url in metadata.gate_evidence_refs),
                ]
            )
        )
        self._enqueue_progress(
            run,
            f"{phase.value}:frozen:{disposition}:{metadata.artifact_digest}",
            self._render_notification(
                run,
                icon="✅",
                title=title,
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    (
                        "审查轮次",
                        (
                            f"{review_attempt}/"
                            f"{self.config.document_review_limit}"
                        ),
                    ),
                    ("冻结结论", escape_markdown(label)),
                    (
                        "工件摘要",
                        inline_code(str(metadata.artifact_digest)[:12]),
                    ),
                ],
                sections=[
                    (
                        "关键决策",
                        [
                            human_summary(decision, limit=180)
                            for decision in metadata.key_decisions[:2]
                        ]
                        or ["无"],
                    ),
                    (
                        "残余风险",
                        [
                            human_summary(risk, limit=180)
                            for risk in metadata.residual_risk[:2]
                        ]
                        or ["无"],
                    ),
                    (
                        "相关链接",
                        [
                            markdown_link(gitlab_link_label(url), url)
                            for url in urls
                        ],
                    ),
                ],
            ),
        )

    def _enqueue_code_retry(
        self,
        run: RunRecord,
        test: CompletionMetadata,
        review: CompletionMetadata | None,
        next_modification: int,
    ) -> None:
        issues = (
            self._code_gate_issues(test, review)
            if review is not None
            else self._test_gate_issues(test)
        )[:6]
        head_sha = review.head_sha if review is not None else test.head_sha
        mr_iid = (
            review.mr_iid if review is not None else test.mr_iid
        ) or test.mr_iid
        mr_url = (
            review.mr_url if review is not None else test.mr_url
        ) or test.mr_url
        self._enqueue_progress(
            run,
            f"code:gates-failed:{head_sha}:modification:{next_modification}",
            self._render_notification(
                run,
                icon="⚠️",
                title=(
                    "CODE 双门禁未同时通过，已退回修改"
                    if review is not None
                    else "CODE 测试未通过，已退回修改"
                ),
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("Head", inline_code(short_sha(head_sha))),
                    ("Tester", format_outcome(test.outcome)),
                    (
                        "Code Reviewer",
                        (
                            format_outcome(review.outcome)
                            if review is not None
                            else "not_run（因测试失败未执行）"
                        ),
                    ),
                    (
                        "MR",
                        markdown_link(
                            f"查看 MR !{mr_iid or 'MR'} 详情",
                            mr_url or "",
                        ),
                    ),
                    (
                        "修改轮次",
                        (
                            f"{next_modification}/"
                            f"{self.config.code_modification_limit}"
                        ),
                    ),
                    ("下一位 Agent", format_agent("coder")),
                    (
                        "后续处理",
                        "代码 push 后先重新执行 Tester；仅通过后进入 Code Review",
                    ),
                ],
                sections=[
                    (
                        "主要问题",
                        [
                            escape_markdown(issue, limit=300)
                            for issue in issues
                        ],
                    )
                ],
            ),
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
            self._render_notification(
                run,
                icon="⚠️",
                title="部分测试条件不可用，已结构化跳过",
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("阶段", format_stage(Stage.TEST)),
                    ("Head", inline_code(short_sha(metadata.head_sha))),
                    (
                        "MR",
                        markdown_link(
                            f"!{metadata.mr_iid or 'MR'}",
                            metadata.mr_url or "",
                        ),
                    ),
                    (
                        "跳过原因",
                        escape_markdown(metadata.skip_reason, limit=500),
                    ),
                    ("后续处理", "Code Reviewer 将继续审查同一提交"),
                ],
                sections=[
                    (
                        "已完成检查",
                        [
                            escape_markdown(item, limit=300)
                            for item in metadata.verification[:3]
                        ]
                        or [escape_markdown(verification, limit=500)],
                    ),
                    (
                        "残余风险",
                        [
                            escape_markdown(item, limit=300)
                            for item in metadata.residual_risk[:3]
                        ]
                        or [escape_markdown(risks, limit=500)],
                    ),
                ],
            ),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="❗",
                    title="自动交付遇到阻塞，需要你的处理",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        ("阶段", format_stage(item.managed.stage)),
                        ("Agent", format_agent(item.task.assignee)),
                        ("Card", inline_code(item.task.id)),
                        (
                            "需要操作",
                            escape_markdown(action, limit=300),
                        ),
                        (
                            "回复方式",
                            (
                                f"回复本消息并 @dispatcher：处理阻塞 "
                                f"{inline_code(run.run_key)} "
                                f"{inline_code(item.task.id)} "
                                f"{inline_code('答案/已完成动作')}"
                            ),
                        ),
                    ],
                    sections=[
                        (
                            "阻塞摘要",
                            [escape_markdown(summary, limit=300)],
                        ),
                        (
                            "判断依据",
                            [escape_markdown(evidence, limit=300)],
                        ),
                    ],
                ),
            ),
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

    def _enqueue_code_flow_completed(
        self,
        run: RunRecord,
        history: list[HistoryItem],
        *,
        terminal_state: str,
        test: CompletionMetadata,
        review: CompletionMetadata | None,
        mr: dict,
        code_modifications: int,
    ) -> None:
        presentation = {
            "completed_ready": (
                "✅",
                "自动开发流程已完成，MR 已就绪",
                "pass（双门禁通过）",
            ),
            "completed_with_findings": (
                "⚠️",
                "自动开发流程已完成，存在审查遗留问题",
                "completed_with_findings（完成但有遗留问题）",
            ),
            "completed_test_failed": (
                "❗",
                "自动开发流程已结束，测试未通过",
                "completed_test_failed（完成但测试未通过）",
            ),
        }
        icon, title, outcome = presentation[terminal_state]
        attempts = self._attempts_by_stage(history)
        stage_summary = [
            (
                "SPEC "
                f"write {attempts.get(Stage.SPEC_WRITE, 0)} / "
                f"review {attempts.get(Stage.SPEC_REVIEW, 0)}"
            ),
            (
                "PLAN "
                f"write {attempts.get(Stage.PLAN_WRITE, 0)} / "
                f"review {attempts.get(Stage.PLAN_REVIEW, 0)}"
            ),
            (
                "TASKS "
                f"write {attempts.get(Stage.TASKS_WRITE, 0)} / "
                f"review {attempts.get(Stage.TASKS_REVIEW, 0)}"
            ),
            (
                "CODE "
                f"implement {attempts.get(Stage.IMPLEMENT, 0)} / "
                f"test {attempts.get(Stage.TEST, 0)} / "
                f"review {attempts.get(Stage.CODE_REVIEW, 0)}"
            ),
        ]
        issues = (
            self._test_gate_issues(test)
            if review is None
            else self._code_gate_issues(test, review)
        )
        if terminal_state == "completed_ready":
            issues = ["Tester 与 Code Reviewer 已验证同一 MR/head。"]
        mr_iid = mr.get("iid") or test.mr_iid
        mr_url = str(mr.get("web_url") or test.mr_url or "")
        mr_ready = terminal_state != "completed_test_failed"
        self._enqueue_progress(
            run,
            f"code:terminal:{terminal_state}:{test.head_sha}",
            self._render_notification(
                run,
                icon=icon,
                title=title,
                fields=[
                    ("任务 ID", inline_code(run.run_key)),
                    ("阶段", format_stage(Phase.CODE)),
                    ("Agent", format_agent("dispatcher")),
                    ("outcome", escape_markdown(outcome)),
                    ("Head", inline_code(short_sha(test.head_sha))),
                    ("Tester", format_outcome(test.outcome)),
                    (
                        "Code Reviewer",
                        (
                            format_outcome(review.outcome)
                            if review is not None
                            else "not_run（因测试失败未执行）"
                        ),
                    ),
                    (
                        "代码修改",
                        (
                            f"{code_modifications}/"
                            f"{self.config.code_modification_limit}"
                        ),
                    ),
                    (
                        "MR",
                        markdown_link(f"查看 MR !{mr_iid} 详情", mr_url),
                    ),
                    (
                        "MR 状态",
                        "Ready（等待人类处理）"
                        if mr_ready
                        else "保持原状态（未改为 Ready）",
                    ),
                    ("合并", "未执行（Controller 不自动合并）"),
                ],
                sections=[
                    ("流程摘要", [escape_markdown(item) for item in stage_summary]),
                    (
                        "结论与遗留问题",
                        [
                            human_summary(issue, limit=240)
                            for issue in issues[:4]
                        ],
                    ),
                    (
                        "下一步",
                        ["请进入 MR 查看代码、审查意见、流水线和审批详情。"],
                    ),
                ],
            ),
            allow_minimal=True,
        )

    def _enqueue_success(self, run: RunRecord, mr: dict) -> None:
        merge_sha = str(mr.get("merge_commit_sha") or "")
        key = f"{run.run_key}:merged:{merge_sha or mr.get('iid')}"
        self.store.enqueue(
            key,
            run.run_key,
            "merged",
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="✅",
                    title="PRD 自动交付完成",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        (
                            "项目",
                            escape_markdown(
                                run.project.project_display_name,
                                limit=200,
                            ),
                        ),
                        (
                            "仓库",
                            inline_code(run.project.project_path),
                        ),
                        (
                            "MR",
                            markdown_link(
                                f"!{mr.get('iid') or 'MR'}",
                                mr.get("web_url") or "",
                            ),
                        ),
                        ("Merge SHA", inline_code(short_sha(merge_sha))),
                        ("结论", "verified（门禁已验证）"),
                    ],
                ),
            ),
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
                markdown_payload(
                    run.origin,
                    self._render_notification(
                        run,
                        icon="⛔",
                        title="废止请求到达前 MR 已完成合并",
                        fields=[
                            ("任务 ID", inline_code(run.run_key)),
                            (
                                "MR",
                                markdown_link(
                                    f"!{mr.get('iid') or mr_iid}",
                                    mr.get("web_url") or "",
                                ),
                            ),
                            ("Head", inline_code(short_sha(checked_head))),
                            (
                                "Merge SHA",
                                inline_code(short_sha(merge_sha)),
                            ),
                            (
                                "状态",
                                "completed_before_abort（合并先于废止完成）",
                            ),
                        ],
                    ),
                ),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="❗",
                    title="流程已合并，但门禁证据无法完整验证",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        (
                            "MR",
                            markdown_link(
                                f"!{mr.get('iid') or mr_iid}",
                                mr.get("web_url") or "",
                            ),
                        ),
                        ("Head", inline_code(short_sha(checked_head))),
                        ("Merge SHA", inline_code(short_sha(merge_sha))),
                        ("合规结论", "unverified（未验证）"),
                        (
                            "后续处理",
                            "状态已终止为 completed，不会再次调度或合并",
                        ),
                    ],
                    sections=[
                        (
                            "原因",
                            [
                                escape_markdown(
                                    compliance_reason,
                                    limit=500,
                                )
                            ],
                        )
                    ],
                ),
            ),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="❗",
                    title="Hermes 工作卡触发失败熔断",
                    fields=[
                        ("任务 ID", inline_code(run.run_key)),
                        (
                            "阶段",
                            format_stage(
                                managed.stage if managed else "unknown"
                            ),
                        ),
                        (
                            "Agent",
                            format_agent(
                                task.assignee if task else "unknown"
                            ),
                        ),
                        ("Card", inline_code(event.task_id)),
                        ("事件", format_event(event.kind)),
                        (
                            "需要操作",
                            "检查该卡的脱敏运行证据，修复环境后查询 status",
                        ),
                    ],
                    sections=[
                        (
                            "运行证据",
                            [escape_markdown(details, limit=500)],
                        )
                    ],
                ),
            ),
        )

    def _enqueue_controller_failure(self, run_key: str, error: Exception) -> None:
        try:
            root = next(
                (
                    card
                    for card in self.store.cards_for_run(run_key)
                    if card.purpose == "root"
                ),
                None,
            )
            if root is None:
                run = self.store.run_record(run_key)
            else:
                task = self.reader.task(root.board, root.card_id)
                run = (
                    parse_run_body(task.body)
                    if task is not None
                    else self.store.run_record(run_key)
                )
            if run is None:
                return
        except (ValidationError, ValueError, json.JSONDecodeError):
            return
        reason = f"{type(error).__name__}: {self._error_text(error)}"
        suffix = hashlib.sha256(reason.encode()).hexdigest()[:12]
        self.store.enqueue(
            f"{run_key}:controller-failure:{suffix}",
            run_key,
            "controller-failure",
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="❗",
                    title="Hollysys Controller 对账失败",
                    fields=[
                        ("任务 ID", inline_code(run_key)),
                        (
                            "需要操作",
                            "修复 Controller/GitLab/Kanban 连通性后运行 health 和 status",
                        ),
                    ],
                    sections=[
                        (
                            "异常证据",
                            [escape_markdown(reason, limit=700)],
                        )
                    ],
                ),
            ),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="⚠️",
                    title="外部依赖暂时不可用，已进入自动退避",
                    fields=[
                        ("任务 ID", inline_code(run_key)),
                        (
                            "依赖",
                            escape_markdown(
                                str(outage["dependency"]).split(":", 1)[0]
                            ),
                        ),
                        ("Outage ID", inline_code(outage_id)),
                        (
                            "错误分类",
                            inline_code(outage["error_class"]),
                        ),
                        ("连续失败", escape_markdown(outage["failures"])),
                        (
                            "下次重试时间",
                            inline_code(outage["next_retry_at"]),
                        ),
                        (
                            "当前状态",
                            "Controller、状态查询及本地服务保持运行，无需重启容器",
                        ),
                    ],
                ),
            ),
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
            markdown_payload(
                run.origin,
                self._render_notification(
                    run,
                    icon="✅",
                    title="外部依赖已恢复，自动交付继续对账",
                    fields=[
                        ("任务 ID", inline_code(run_key)),
                        (
                            "依赖",
                            escape_markdown(
                                str(outage["dependency"]).split(":", 1)[0]
                            ),
                        ),
                        ("Outage ID", inline_code(outage_id)),
                        ("中断耗时", format_duration(duration)),
                        ("重试次数", escape_markdown(outage["failures"])),
                        (
                            "继续方式",
                            "沿用原 run、worktree 和 MR 继续对账",
                        ),
                    ],
                ),
            ),
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
