from __future__ import annotations

from dataclasses import dataclass


class ControllerError(RuntimeError):
    """Base class for errors whose recovery policy is part of the protocol."""


@dataclass(frozen=True)
class ErrorContext:
    dependency: str
    endpoint: str | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None
    error_code: str | None = None


class DependencyError(ControllerError):
    error_class = "dependency"

    def __init__(self, message: str, *, context: ErrorContext):
        super().__init__(message)
        self.context = context


class DependencyTransientError(DependencyError):
    error_class = "dependency_transient"


class DependencyRateLimitedError(DependencyError):
    error_class = "dependency_rate_limited"


class DependencyAuthError(DependencyError):
    error_class = "dependency_auth"


class DependencyContractError(DependencyError):
    error_class = "dependency_contract"


class RunPolicyError(ControllerError):
    """A run-specific policy failure that must not open a host circuit."""


class ReconcileSuperseded(ControllerError):
    """The run changed while reconcile was doing lock-free external I/O."""


class ControllerFatalError(ControllerError):
    """A local invariant/store/configuration failure requiring supervisor action."""


class UncertainOperation(ControllerError):
    """An external mutation may have succeeded and must be reconciled first."""


class MergeBlocked(RunPolicyError):
    def __init__(
        self,
        kind: str,
        message: str,
        *,
        url: str | None = None,
        owner: str | None = None,
        updated_at: str | None = None,
        immediate_exception: bool = False,
    ):
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.owner = owner
        self.updated_at = updated_at
        self.immediate_exception = immediate_exception
