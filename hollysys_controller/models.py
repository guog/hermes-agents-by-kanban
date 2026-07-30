from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

RUN_KEY_BODY_PATTERN = r"[a-z0-9]{20}"
RUN_KEY_PATTERN = rf"^hollysys-{RUN_KEY_BODY_PATTERN}$"
SOURCE_KEY_PATTERN = rf"^source-{RUN_KEY_BODY_PATTERN}$"
RUN_GENERATION_PATTERN = rf"^{RUN_KEY_BODY_PATTERN}$"
CARD_ID_PATTERN = r"^t_[A-Za-z0-9_-]+$"
BLOCK_ID_PATTERN = rf"^hollysys-{RUN_KEY_BODY_PATTERN}:t_[A-Za-z0-9_-]+:[1-9][0-9]*$"
SHA_PATTERN = r"^[0-9a-f]{40}$"
DIGEST_PATTERN = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Stage(StrEnum):
    SPEC_WRITE = "spec-write"
    SPEC_REVIEW = "spec-review"
    PLAN_WRITE = "plan-write"
    PLAN_REVIEW = "plan-review"
    TASKS_WRITE = "tasks-write"
    TASKS_REVIEW = "tasks-review"
    IMPLEMENT = "implement"
    TEST = "test"
    CODE_REVIEW = "code-review"


class Phase(StrEnum):
    SPEC = "spec"
    PLAN = "plan"
    TASKS = "tasks"
    CODE = "code"


class WorkMode(StrEnum):
    NORMAL = "normal"
    FINALIZATION = "finalization"


class RepairKind(StrEnum):
    REVIEW_FAILURE = "review_failure"
    CODE_GATE_FAILURE = "code_gate_failure"
    FROZEN_ARTIFACT_VIOLATION = "frozen_artifact_violation"


class BaselineDisposition(StrEnum):
    SOURCE = "source"
    REVIEWED = "reviewed"
    FORCED_AFTER_REVIEW_LIMIT = "forced_after_review_limit"


class TestDisposition(StrEnum):
    EXECUTED = "executed"
    SKIPPED_UNAVAILABLE = "skipped_unavailable"


class GatePhase(StrEnum):
    IMPLEMENTATION_ENTRY = "implementation_entry"
    IMPLEMENTATION_COMPLETION = "implementation_completion"
    MIGRATION_EXECUTION = "migration_execution"
    DEPLOYMENT_ENTRY = "deployment_entry"
    RELEASE_ACCEPTANCE = "release_acceptance"


class GateDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class NotificationLevel(StrEnum):
    VERBOSE = "verbose"
    STANDARD = "standard"
    MINIMAL = "minimal"


class ChangeStrategy(StrEnum):
    EXTEND_EXISTING = "extend_existing"
    MODIFY_EXISTING = "modify_existing"
    EXTEND_AND_MODIFY = "extend_and_modify"


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    CANCELLED = "cancelled"


class FeishuOrigin(StrictModel):
    platform: Literal["feishu"] = "feishu"
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None
    chat_type: Literal["group", "p2p"]
    initiator_open_id: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]


class ProjectFacts(StrictModel):
    host: str
    project_id: Annotated[int, Field(gt=0)]
    project_path: str
    project_display_name: str
    default_branch: str


class SourceFacts(StrictModel):
    prd_path: str
    prd_commit_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    prd_blob_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    prd_blob_url: AnyHttpUrl
    prd_mr_url: AnyHttpUrl


class WorkspaceFacts(StrictModel):
    board: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    checkout: str
    worktree: str
    branch: str
    target_branch: str
    repository_base_sha: Annotated[str, Field(pattern=SHA_PATTERN)]


class RunRecord(StrictModel):
    protocol_version: Literal["hollysys-controller/v4"] = "hollysys-controller/v4"
    kind: Literal["run-init"] = "run-init"
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    source_key: Annotated[str, Field(pattern=SOURCE_KEY_PATTERN)]
    run_generation: Annotated[str, Field(pattern=RUN_GENERATION_PATTERN)]
    started_at: datetime
    provenance: Literal["fresh_v4"] = "fresh_v4"
    project: ProjectFacts
    source: SourceFacts
    workspace: WorkspaceFacts
    origin: FeishuOrigin

    @model_validator(mode="after")
    def validate_started_at(self) -> RunRecord:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must include a timezone")
        return self


class DeliveryBinding(StrictModel):
    mr_iid: Annotated[int, Field(gt=0)]
    mr_url: AnyHttpUrl
    creator: Annotated[str, Field(min_length=1)]
    created_at: datetime
    initial_head_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    claim_note_id: Annotated[int, Field(gt=0)]

    @model_validator(mode="after")
    def validate_created_at(self) -> DeliveryBinding:
        if self.created_at.tzinfo is None:
            raise ValueError("delivery created_at must include a timezone")
        return self


class ArtifactBaseline(StrictModel):
    phase: Literal["prd", "spec", "plan", "tasks"]
    disposition: BaselineDisposition
    artifact_paths: list[str] = Field(min_length=1)
    artifact_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]
    artifact_commit_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    source_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    decision_urls: list[AnyHttpUrl] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    unresolved_findings: list[str] = Field(default_factory=list)
    residual_risk: list[str] = Field(default_factory=list)


class RepairContext(StrictModel):
    kind: RepairKind
    trigger_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    issues: list[str] = Field(min_length=1)
    review_attempt: Annotated[int | None, Field(ge=1)] = None
    review_limit: Annotated[int | None, Field(ge=1)] = None
    related_card_ids: list[
        Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    ] = Field(default_factory=list)
    head_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    code_modification: Annotated[int | None, Field(ge=1)] = None
    code_modification_limit: Annotated[int | None, Field(ge=1)] = None
    frozen_baselines: list[ArtifactBaseline] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_repair_contract(self) -> RepairContext:
        if self.kind == RepairKind.REVIEW_FAILURE and (
            self.review_attempt is None or self.review_limit is None
        ):
            raise ValueError(
                "review_failure requires review_attempt and review_limit"
            )
        if self.kind != RepairKind.REVIEW_FAILURE and (
            self.review_attempt is not None or self.review_limit is not None
        ):
            raise ValueError(
                "review attempt fields are only valid for review_failure"
            )
        if self.kind == RepairKind.CODE_GATE_FAILURE and (
            not self.related_card_ids
            or self.head_sha is None
            or self.code_modification is None
            or self.code_modification_limit is None
        ):
            raise ValueError(
                "code_gate_failure requires both gate cards, head, and "
                "modification counters"
            )
        if self.code_modification is not None and (
            self.code_modification_limit is None
            or self.code_modification > self.code_modification_limit
        ):
            raise ValueError(
                "code_modification cannot exceed code_modification_limit"
            )
        if self.kind != RepairKind.CODE_GATE_FAILURE and (
            self.related_card_ids
            or self.head_sha is not None
            or self.code_modification is not None
            or self.code_modification_limit is not None
        ):
            raise ValueError(
                "code gate fields are only valid for code_gate_failure"
            )
        if (
            self.review_attempt is not None
            and self.review_limit is not None
            and self.review_attempt > self.review_limit
        ):
            raise ValueError("review_attempt cannot exceed review_limit")
        return self


class CardRecord(StrictModel):
    protocol_version: Literal["hollysys-controller/v4"] = "hollysys-controller/v4"
    kind: Literal["work"] = "work"
    run: RunRecord
    stage: Stage
    iteration: Annotated[int, Field(ge=1)]
    mode: WorkMode = WorkMode.NORMAL
    idempotency_key: str
    parent_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    assignee: str
    skills: list[str]
    frozen_baselines: list[ArtifactBaseline] = Field(default_factory=list)
    repair_context: RepairContext | None = None
    resume_answer: str | None = None
    resumed_from_card_id: str | None = None
    expected_head_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    context_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]
    scratch_dir: str
    delivery: DeliveryBinding | None = None

    @model_validator(mode="after")
    def validate_scratch_dir(self) -> CardRecord:
        path = PurePosixPath(self.scratch_dir)
        root = PurePosixPath("/opt/data/scratch")
        if (
            not path.is_absolute()
            or path == root
            or path.parts[: len(root.parts)] != root.parts
            or ".." in path.parts
        ):
            raise ValueError(
                "scratch_dir must be a child of /opt/data/scratch"
            )
        return self


class ForcedAdvance(StrictModel):
    review_limit: Annotated[int, Field(ge=1)]
    final_review_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    final_review_url: AnyHttpUrl
    decision_url: AnyHttpUrl
    baseline_commit_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    artifact_paths: list[str] = Field(min_length=1)
    artifact_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]
    key_decisions: list[str] = Field(min_length=1)
    unresolved_findings: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)


class RepositoryEvidence(StrictModel):
    repository_base_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    inspected_paths: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1
    )
    existing_capabilities: list[
        Annotated[str, Field(min_length=1)]
    ] = Field(min_length=1)
    change_strategy: ChangeStrategy
    reuse_decisions: list[Annotated[str, Field(min_length=1)]] = Field(
        min_length=1
    )

    @model_validator(mode="after")
    def validate_repository_paths(self) -> RepositoryEvidence:
        if len(set(self.inspected_paths)) != len(self.inspected_paths):
            raise ValueError("inspected_paths must be unique")
        for raw_path in self.inspected_paths:
            path = PurePosixPath(raw_path)
            if (
                path.is_absolute()
                or raw_path in {"", "."}
                or ".." in path.parts
                or any(token in raw_path for token in ("*", "?", "[", "]", "\0"))
            ):
                raise ValueError(
                    "inspected_paths must be exact repository-relative paths"
                )
        return self


class DeterministicCheck(StrictModel):
    validator: Annotated[str, Field(min_length=1)]
    validator_version: Annotated[str, Field(min_length=1)]
    input_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]
    passed: bool
    error_codes: list[Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]]
    result_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]

    @model_validator(mode="after")
    def validate_result(self) -> DeterministicCheck:
        if self.passed and self.error_codes:
            raise ValueError("passed deterministic check cannot have errors")
        if not self.passed and not self.error_codes:
            raise ValueError("failed deterministic check requires error_codes")
        if len(self.error_codes) != len(set(self.error_codes)):
            raise ValueError("deterministic error_codes must be unique")
        return self


class CompletionMetadata(StrictModel):
    protocol_version: Literal["hollysys-controller/v4"]
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    source_key: Annotated[str, Field(pattern=SOURCE_KEY_PATTERN)]
    run_generation: Annotated[str, Field(pattern=RUN_GENERATION_PATTERN)]
    context_digest: Annotated[str, Field(pattern=DIGEST_PATTERN)]
    stage: Stage
    iteration: Annotated[int, Field(ge=1)]
    mode: WorkMode = WorkMode.NORMAL
    outcome: Outcome

    project_id: Annotated[int, Field(gt=0)]
    project_path: str
    checkout: str
    worktree: str
    branch: str
    target_branch: str
    prd_path: str
    prd_commit_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    prd_blob_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    prd_mr_url: AnyHttpUrl
    kanban_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]

    mr_iid: Annotated[int | None, Field(gt=0)] = None
    mr_url: AnyHttpUrl | None = None
    head_before_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)]
    head_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    artifact_commit_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    artifact_digest: Annotated[str | None, Field(pattern=DIGEST_PATTERN)] = None
    artifact_paths: list[str] = Field(default_factory=list)
    baseline_disposition: BaselineDisposition | None = None
    forced_advance: ForcedAdvance | None = None
    repository_evidence: RepositoryEvidence | None = None
    test_disposition: TestDisposition | None = None
    skip_reason: Annotated[str | None, Field(min_length=1)] = None
    verification: list[str] = Field(default_factory=list)
    gitlab_urls: list[AnyHttpUrl] = Field(default_factory=list)
    key_decisions: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    residual_risk: list[str] = Field(default_factory=list)
    gate_phase: GatePhase | None = None
    gate_decision: GateDecision | None = None
    gate_reviewer: Annotated[
        str | None,
        Field(pattern=r"^id:[1-9][0-9]*$"),
    ] = None
    gate_reviewed_at: datetime | None = None
    gate_reason: Annotated[str | None, Field(min_length=1)] = None
    gate_evidence_refs: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    gate_artifact_paths: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    gate_artifact_commit_sha: Annotated[
        str | None,
        Field(pattern=SHA_PATTERN),
    ] = None
    gate_artifact_digest: Annotated[
        str | None,
        Field(pattern=DIGEST_PATTERN),
    ] = None
    contract_refs: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    requirement_ids: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    deterministic_checks: list[DeterministicCheck]

    @model_validator(mode="after")
    def validate_stage_contract(self) -> CompletionMetadata:
        gate_fields_present = any(
            (
                self.gate_decision is not None,
                self.gate_reviewer is not None,
                self.gate_reviewed_at is not None,
                self.gate_reason is not None,
                bool(self.gate_evidence_refs),
                bool(self.gate_artifact_paths),
                self.gate_artifact_commit_sha is not None,
                self.gate_artifact_digest is not None,
                bool(self.contract_refs),
                bool(self.requirement_ids),
            )
        )
        if self.gate_phase is not None and (
            self.gate_decision is None
            or self.gate_reviewer is None
            or self.gate_reviewed_at is None
            or self.gate_reason is None
            or not self.gate_evidence_refs
            or not self.gate_artifact_paths
            or self.gate_artifact_commit_sha is None
            or self.gate_artifact_digest is None
            or not self.contract_refs
            or not self.requirement_ids
        ):
            raise ValueError(
                "gate_phase requires decision, reviewer, reviewed_at, reason, "
                "evidence refs, frozen artifact paths/version/digest, "
                "contract_refs and requirement_ids"
            )
        if self.gate_phase is None and gate_fields_present:
            raise ValueError(
                "gate evidence fields require gate_phase"
            )
        if (
            self.gate_phase is not None
            and self.gate_decision is not None
            and self.gate_reviewed_at is not None
        ):
            if self.gate_reviewed_at.tzinfo is None:
                raise ValueError("gate_reviewed_at must include a timezone")
            if self.gate_decision == GateDecision.APPROVED:
                if self.outcome != Outcome.PASS:
                    raise ValueError("approved gate requires outcome=pass")
            elif self.outcome != Outcome.FAIL:
                raise ValueError("rejected gate requires outcome=fail")
            for values, name in (
                (self.requirement_ids, "requirement_ids"),
                (self.contract_refs, "contract_refs"),
                (self.gate_evidence_refs, "gate_evidence_refs"),
                (self.gate_artifact_paths, "gate_artifact_paths"),
            ):
                if len(values) != len(set(values)):
                    raise ValueError(f"{name} must be unique")
            for raw_path in self.gate_artifact_paths:
                path = PurePosixPath(raw_path)
                if (
                    path.is_absolute()
                    or raw_path in {"", "."}
                    or ".." in path.parts
                    or any(
                        token in raw_path
                        for token in ("*", "?", "[", "]", "\0")
                    )
                ):
                    raise ValueError(
                        "gate_artifact_paths must be exact repository paths"
                    )
        document_reviews = {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
        }
        document_producers = {
            Stage.SPEC_WRITE,
            Stage.PLAN_WRITE,
            Stage.TASKS_WRITE,
        }
        repository_authors = document_producers | {Stage.IMPLEMENT}
        code_gates = {Stage.TEST, Stage.CODE_REVIEW}

        if (
            self.stage == Stage.TASKS_REVIEW
            and self.outcome == Outcome.PASS
            and (
                self.gate_phase != GatePhase.IMPLEMENTATION_ENTRY
                or self.gate_decision != GateDecision.APPROVED
            )
        ):
            raise ValueError(
                "tasks-review pass requires approved implementation_entry gate"
            )
        if (
            self.stage == Stage.CODE_REVIEW
            and self.outcome == Outcome.PASS
            and (
                self.gate_phase != GatePhase.IMPLEMENTATION_COMPLETION
                or self.gate_decision != GateDecision.APPROVED
            )
        ):
            raise ValueError(
                "code-review pass requires approved "
                "implementation_completion gate"
            )

        if self.outcome == Outcome.FAIL and not self.issues:
            raise ValueError("fail requires a non-empty issues list")
        if self.outcome == Outcome.FAIL and self.stage not in (
            document_reviews | code_gates
        ):
            raise ValueError("only review/test stages may return fail")

        if (
            self.outcome in {Outcome.PASS, Outcome.FAIL}
            and self.stage in document_reviews
            and (
                not self.artifact_paths
                or self.artifact_digest is None
                or self.artifact_commit_sha is None
            )
        ):
            raise ValueError(
                "artifact review requires paths, digest, and artifact_commit_sha"
            )

        if (
            self.outcome in {Outcome.PASS, Outcome.FAIL}
            and self.stage in code_gates
            and (self.mr_iid is None or self.mr_url is None or self.head_sha is None)
        ):
            raise ValueError(
                "test/code-review requires mr_iid, mr_url, and head_sha"
            )

        if self.stage == Stage.TEST and self.outcome in {
            Outcome.PASS,
            Outcome.FAIL,
        }:
            if self.test_disposition is None:
                raise ValueError(
                    "test pass/fail requires test_disposition"
                )
            if self.test_disposition == TestDisposition.SKIPPED_UNAVAILABLE:
                if (
                    self.outcome != Outcome.PASS
                    or self.skip_reason is None
                    or not self.verification
                    or not self.residual_risk
                ):
                    raise ValueError(
                        "skipped test requires pass, skip_reason, "
                        "verification evidence, and residual risk"
                    )
            elif self.skip_reason is not None:
                raise ValueError(
                    "executed test must not include skip_reason"
                )
        elif self.test_disposition is not None or self.skip_reason is not None:
            raise ValueError(
                "test disposition fields are only valid for test pass/fail"
            )

        if self.outcome == Outcome.PASS and self.stage in repository_authors:
            if self.repository_evidence is None:
                raise ValueError(
                    "SPEC/PLAN/TASKS/CODE authoring pass requires "
                    "repository_evidence"
                )
            if self.mr_iid is None or self.mr_url is None or self.head_sha is None:
                raise ValueError(
                    "SPEC/PLAN/TASKS/CODE authoring pass requires the "
                    "shared delivery MR and current head"
                )
        elif self.repository_evidence is not None:
            raise ValueError(
                "repository_evidence is only valid for an authoring pass"
            )

        if self.mode == WorkMode.FINALIZATION:
            if self.stage not in document_producers:
                raise ValueError(
                    "finalization is only valid for a document producer"
                )
            if self.outcome == Outcome.PASS and (
                not self.artifact_paths
                or self.artifact_digest is None
                or self.artifact_commit_sha is None
                or self.mr_iid is None
                or self.mr_url is None
                or self.baseline_disposition
                != BaselineDisposition.FORCED_AFTER_REVIEW_LIMIT
                or self.forced_advance is None
            ):
                raise ValueError(
                    "finalization requires a forced artifact baseline and decision"
                )
            if (
                self.outcome == Outcome.PASS
                and self.forced_advance is not None
                and (
                    self.forced_advance.baseline_commit_sha
                    != self.artifact_commit_sha
                    or self.forced_advance.artifact_paths
                    != self.artifact_paths
                    or self.forced_advance.artifact_digest
                    != self.artifact_digest
                    or self.forced_advance.key_decisions
                    != self.key_decisions
                    or self.forced_advance.residual_risks
                    != self.residual_risk
                )
            ):
                raise ValueError(
                    "forced_advance baseline and residual risks must match "
                    "top-level finalization evidence"
                )
            if self.outcome not in {Outcome.PASS, Outcome.CANCELLED}:
                raise ValueError("finalization only allows pass or cancelled")
        elif self.forced_advance is not None:
            raise ValueError("forced_advance is only valid for finalization")

        if self.stage in document_reviews and self.outcome == Outcome.PASS:
            if self.baseline_disposition != BaselineDisposition.REVIEWED:
                raise ValueError(
                    "document review pass requires baseline_disposition=reviewed"
                )
        elif (
            self.mode != WorkMode.FINALIZATION
            and self.baseline_disposition is not None
        ):
            raise ValueError(
                "baseline_disposition is only valid for a document review pass "
                "or finalization"
            )
        return self


def validate_persisted_completion_metadata(raw: object) -> CompletionMetadata:
    """Validate the worker payload after separating the trusted runtime envelope.

    Hermes v2026.7.20 appends ``worker_session_id`` after a scoped worker calls
    ``kanban_complete``.  It is runtime provenance, not part of the worker's
    strict business completion contract.
    """
    if not isinstance(raw, dict):
        return CompletionMetadata.model_validate(raw)
    payload = dict(raw)
    if "worker_session_id" in payload:
        worker_session_id = payload.pop("worker_session_id")
        if not isinstance(worker_session_id, str) or not worker_session_id.strip():
            raise ValueError("worker_session_id must be a non-empty string")
    return CompletionMetadata.model_validate(payload)


class StartRequest(StrictModel):
    prd_blob_url: AnyHttpUrl
    prd_mr_url: AnyHttpUrl
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None
    chat_type: Literal["group", "p2p"]
    initiator: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]


class ResolveRequest(StrictModel):
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    block_id: Annotated[str, Field(pattern=BLOCK_ID_PATTERN)]
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    sender: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None
    answer: Annotated[str, Field(min_length=1, max_length=4000)]


class RecoverRequest(StrictModel):
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    sender: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class AbortRequest(StrictModel):
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    sender: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class AbortConfirmRequest(StrictModel):
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    token: Annotated[str, Field(pattern=r"^[A-Z2-9]{8}$")]
    message_id: Annotated[str, Field(pattern=r"^om_[A-Za-z0-9_-]+$")]
    sender: Annotated[str, Field(pattern=r"^ou_[A-Za-z0-9_-]+$")]
    chat_id: Annotated[str, Field(pattern=r"^oc_[A-Za-z0-9_-]+$")]
    thread_id: str | None = None


class CompletionValidationRequest(StrictModel):
    card_id: Annotated[str, Field(min_length=1)]
    metadata: dict


class RpcRequest(StrictModel):
    id: str
    method: Literal[
        "start",
        "status",
        "status-summary",
        "resolve",
        "recover",
        "health",
        "preflight",
        "abort-request",
        "abort-confirm",
        "validate-completion",
        "publish-delivery",
        "card-context",
        "completion-template",
        "validate-artifact",
    ]
    params: dict = Field(default_factory=dict)


class RpcResponse(StrictModel):
    id: str
    ok: bool
    result: dict | None = None
    error: str | None = None
