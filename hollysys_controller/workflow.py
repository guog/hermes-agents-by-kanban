from __future__ import annotations

from dataclasses import dataclass

from .config import ControllerConfig
from .models import CompletionMetadata, Outcome, Stage

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


@dataclass(frozen=True)
class Route:
    next_stage: Stage | None = None
    merge: bool = False
    blocked_reason: str | None = None


def next_stage(stage: Stage) -> Stage | None:
    index = SEQUENCE.index(stage)
    return SEQUENCE[index + 1] if index + 1 < len(SEQUENCE) else None


def route_completion(
    metadata: CompletionMetadata,
    *,
    attempts_by_stage: dict[Stage, int],
    config: ControllerConfig,
) -> Route:
    if metadata.outcome == Outcome.CANCELLED:
        if metadata.stage in {Stage.TEST, Stage.CODE_REVIEW}:
            return Route(next_stage=Stage.TEST)
        return Route(blocked_reason=f"{metadata.stage} was cancelled")

    if metadata.outcome == Outcome.SCOPE_GAP:
        assert metadata.scope_gap_target is not None
        return _budgeted(
            Stage(metadata.scope_gap_target),
            attempts_by_stage,
            config,
        )

    if metadata.outcome == Outcome.FAIL:
        target = PRODUCER_FOR_REVIEW.get(metadata.stage, metadata.stage)
        return _budgeted(target, attempts_by_stage, config)

    following = next_stage(metadata.stage)
    return Route(merge=True) if following is None else Route(next_stage=following)


def _budgeted(
    target: Stage,
    attempts_by_stage: dict[Stage, int],
    config: ControllerConfig,
) -> Route:
    # Initial attempt plus N rework attempts.
    if (
        target in {Stage.SPEC_WRITE, Stage.PLAN_WRITE, Stage.TASKS_WRITE}
        and attempts_by_stage.get(target, 0) >= 1 + config.design_rework_limit
    ):
        return Route(blocked_reason=f"{target} rework budget exhausted")
    if (
        target == Stage.IMPLEMENT
        and attempts_by_stage.get(target, 0) >= 1 + config.code_rework_limit
    ):
        return Route(blocked_reason="code rework budget exhausted")
    return Route(next_stage=target)


def iteration_for(stage: Stage, attempts_by_stage: dict[Stage, int]) -> int:
    return attempts_by_stage.get(stage, 0) + 1


def protocol_retry_allowed(invalid_count: int, config: ControllerConfig) -> bool:
    return invalid_count <= config.protocol_retry_limit
