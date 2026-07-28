from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

RUN_KEY_BODY_PATTERN = r"[a-z0-9]{20}"
RUN_KEY_PATTERN = rf"^hollysys-{RUN_KEY_BODY_PATTERN}$"
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


class Outcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SCOPE_GAP = "scope_gap"
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
    prd_blob_url: AnyHttpUrl
    prd_mr_url: AnyHttpUrl


class WorkspaceFacts(StrictModel):
    board: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
    checkout: str
    worktree: str
    branch: str
    target_branch: str


class RunRecord(StrictModel):
    protocol_version: Literal["hollysys-controller/v1"] = "hollysys-controller/v1"
    kind: Literal["run-init"] = "run-init"
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    project: ProjectFacts
    source: SourceFacts
    workspace: WorkspaceFacts
    origin: FeishuOrigin


class CardRecord(StrictModel):
    protocol_version: Literal["hollysys-controller/v1"] = "hollysys-controller/v1"
    kind: Literal["work"] = "work"
    run: RunRecord
    stage: Stage
    iteration: Annotated[int, Field(ge=1)]
    idempotency_key: str
    parent_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]
    assignee: str
    skills: list[str]
    resume_answer: str | None = None
    resumed_from_card_id: str | None = None


class CompletionMetadata(StrictModel):
    protocol_version: Literal["hollysys-controller/v1"]
    run_key: Annotated[str, Field(pattern=RUN_KEY_PATTERN)]
    stage: Stage
    iteration: Annotated[int, Field(ge=1)]
    outcome: Outcome

    project_id: Annotated[int, Field(gt=0)]
    project_path: str
    checkout: str
    worktree: str
    branch: str
    target_branch: str
    prd_path: str
    prd_commit_sha: Annotated[str, Field(pattern=SHA_PATTERN)]
    prd_mr_url: AnyHttpUrl
    kanban_card_id: Annotated[str, Field(pattern=CARD_ID_PATTERN)]

    mr_iid: Annotated[int | None, Field(gt=0)] = None
    mr_url: AnyHttpUrl | None = None
    head_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    review_commit_sha: Annotated[str | None, Field(pattern=SHA_PATTERN)] = None
    artifact_digest: Annotated[str | None, Field(pattern=DIGEST_PATTERN)] = None
    artifact_paths: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    gitlab_urls: list[AnyHttpUrl] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    scope_gap_target: Literal["spec-write", "plan-write", "tasks-write"] | None = None
    residual_risk: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_stage_contract(self) -> CompletionMetadata:
        if self.outcome == Outcome.SCOPE_GAP:
            if self.scope_gap_target is None or not self.issues:
                raise ValueError(
                    "scope_gap requires scope_gap_target and a non-empty issues list"
                )
        elif self.scope_gap_target is not None:
            raise ValueError("scope_gap_target is only valid for outcome=scope_gap")

        if (
            self.outcome == Outcome.PASS
            and self.stage
            in {
                Stage.SPEC_REVIEW,
                Stage.PLAN_REVIEW,
                Stage.TASKS_REVIEW,
            }
            and (
                not self.artifact_paths
                or self.artifact_digest is None
                or self.review_commit_sha is None
            )
        ):
            raise ValueError(
                "artifact review pass requires paths, digest, and review_commit_sha"
            )

        if (
            self.outcome == Outcome.PASS
            and self.stage in {Stage.TEST, Stage.CODE_REVIEW}
            and (self.mr_iid is None or self.mr_url is None or self.head_sha is None)
        ):
            raise ValueError(
                "test/code-review pass requires mr_iid, mr_url, and head_sha"
            )
        return self


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


class RpcRequest(StrictModel):
    id: str
    method: Literal["start", "status", "resolve", "health"]
    params: dict = Field(default_factory=dict)


class RpcResponse(StrictModel):
    id: str
    ok: bool
    result: dict | None = None
    error: str | None = None
