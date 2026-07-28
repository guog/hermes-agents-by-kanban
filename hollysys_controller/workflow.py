from __future__ import annotations

from dataclasses import dataclass

from .config import ControllerConfig
from .models import CompletionMetadata, Outcome, Phase, Stage, WorkMode

SEQUENCE = (
    Stage.SPEC_WRITE,
    Stage.SPEC_REVIEW,
    Stage.PLAN_WRITE,
    Stage.PLAN_REVIEW,
    Stage.TASKS_WRITE,
    Stage.TASKS_REVIEW,
    Stage.IMPLEMENT,
    Stage.TEST,
    Stage.CODE_REVIEW,
)

PRODUCER_FOR_REVIEW = {
    Stage.SPEC_REVIEW: Stage.SPEC_WRITE,
    Stage.PLAN_REVIEW: Stage.PLAN_WRITE,
    Stage.TASKS_REVIEW: Stage.TASKS_WRITE,
    Stage.TEST: Stage.IMPLEMENT,
    Stage.CODE_REVIEW: Stage.IMPLEMENT,
}

DOCUMENT_REVIEW_FOR_PRODUCER = {
    Stage.SPEC_WRITE: Stage.SPEC_REVIEW,
    Stage.PLAN_WRITE: Stage.PLAN_REVIEW,
    Stage.TASKS_WRITE: Stage.TASKS_REVIEW,
}

PHASE_FOR_STAGE = {
    Stage.SPEC_WRITE: Phase.SPEC,
    Stage.SPEC_REVIEW: Phase.SPEC,
    Stage.PLAN_WRITE: Phase.PLAN,
    Stage.PLAN_REVIEW: Phase.PLAN,
    Stage.TASKS_WRITE: Phase.TASKS,
    Stage.TASKS_REVIEW: Phase.TASKS,
    Stage.IMPLEMENT: Phase.CODE,
    Stage.TEST: Phase.CODE,
    Stage.CODE_REVIEW: Phase.CODE,
}

PRODUCER_FOR_PHASE = {
    Phase.SPEC: Stage.SPEC_WRITE,
    Phase.PLAN: Stage.PLAN_WRITE,
    Phase.TASKS: Stage.TASKS_WRITE,
    Phase.CODE: Stage.IMPLEMENT,
}

NEXT_PHASE_STAGE = {
    Phase.SPEC: Stage.PLAN_WRITE,
    Phase.PLAN: Stage.TASKS_WRITE,
    Phase.TASKS: Stage.IMPLEMENT,
}


@dataclass(frozen=True)
class Route:
    next_stage: Stage | None = None
    next_mode: WorkMode = WorkMode.NORMAL
    merge: bool = False
    blocked_reason: str | None = None


def next_stage(stage: Stage) -> Stage | None:
    index = SEQUENCE.index(stage)
    return SEQUENCE[index + 1] if index + 1 < len(SEQUENCE) else None


def route_completion(
    metadata: CompletionMetadata,
    *,
    review_attempts_by_stage: dict[Stage, int],
    config: ControllerConfig,
    paired_test: CompletionMetadata | None = None,
    code_modifications: int = 0,
) -> Route:
    if metadata.outcome == Outcome.CANCELLED:
        if metadata.stage in {Stage.TEST, Stage.CODE_REVIEW}:
            return Route(next_stage=Stage.TEST)
        return Route(blocked_reason=f"{metadata.stage} was cancelled")

    # Tester and code-reviewer always inspect the same implementation head.
    # A failed test does not short-circuit code review: both sets of findings
    # are collected before deciding whether coder must modify the code.
    if metadata.stage == Stage.TEST:
        return Route(next_stage=Stage.CODE_REVIEW)

    if metadata.stage == Stage.CODE_REVIEW:
        same_delivery = (
            paired_test is not None
            and paired_test.mr_iid == metadata.mr_iid
            and paired_test.mr_url == metadata.mr_url
            and paired_test.head_sha == metadata.head_sha
        )
        if not same_delivery:
            return Route(next_stage=Stage.TEST)
        if (
            paired_test.outcome == Outcome.PASS
            and metadata.outcome == Outcome.PASS
        ):
            return Route(merge=True)
        if code_modifications >= config.code_modification_limit:
            return Route(
                blocked_reason=(
                    "code modification limit "
                    f"{config.code_modification_limit} exhausted"
                )
            )
        return Route(next_stage=Stage.IMPLEMENT)

    if metadata.outcome == Outcome.FAIL:
        target = PRODUCER_FOR_REVIEW.get(metadata.stage)
        if target is None:
            return Route(blocked_reason=f"{metadata.stage} cannot return fail")
        if metadata.stage in {
            Stage.SPEC_REVIEW,
            Stage.PLAN_REVIEW,
            Stage.TASKS_REVIEW,
        }:
            review_attempt = review_attempts_by_stage.get(metadata.stage, 0)
            mode = (
                WorkMode.FINALIZATION
                if review_attempt >= config.document_review_limit
                else WorkMode.NORMAL
            )
            return Route(next_stage=target, next_mode=mode)
        return Route(next_stage=target)

    if metadata.mode == WorkMode.FINALIZATION:
        phase = PHASE_FOR_STAGE[metadata.stage]
        following = NEXT_PHASE_STAGE.get(phase)
        return Route(merge=True) if following is None else Route(next_stage=following)

    following = next_stage(metadata.stage)
    return Route(merge=True) if following is None else Route(next_stage=following)


def iteration_for(stage: Stage, attempts_by_stage: dict[Stage, int]) -> int:
    return attempts_by_stage.get(stage, 0) + 1


def protocol_retry_allowed(invalid_count: int, config: ControllerConfig) -> bool:
    return invalid_count <= config.protocol_retry_limit
