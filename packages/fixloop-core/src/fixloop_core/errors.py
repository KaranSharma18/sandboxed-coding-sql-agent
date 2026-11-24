"""Exception hierarchy.

Every failure the loop can end with maps to one exception type, and every exception type maps
to exactly one task outcome or HTTP status. Keeping that mapping explicit is what lets the
driver turn "something went wrong" into a precise, user-facing reason.
"""

from __future__ import annotations

from typing import Any


class FixloopError(Exception):
    """Base class. `code` is stable and machine-readable; the message is for humans."""

    code = "error"

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.__class__.__name__)
        self.message = message or self.__class__.__name__
        self.details = details


class InvalidInput(FixloopError):
    """The task cannot run as specified (missing target test, bad archive, nothing collected)."""

    code = "invalid_input"


class TooLarge(FixloopError):
    """An input is over a size limit (HTTP 413)."""

    code = "too_large"


class NotFound(FixloopError):
    code = "not_found"


class Conflict(FixloopError):
    code = "conflict"


class QuotaExceeded(FixloopError):
    """A tenant limit was hit at intake (HTTP 429)."""

    code = "quota_exceeded"

    def __init__(self, message: str = "", retry_after: int = 60, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class Overloaded(FixloopError):
    """The platform is shedding load (HTTP 503)."""

    code = "overloaded"

    def __init__(self, message: str = "", retry_after: int = 30, **details: Any) -> None:
        super().__init__(message, **details)
        self.retry_after = retry_after


class Unauthorized(FixloopError):
    code = "unauthorized"


class Forbidden(FixloopError):
    code = "forbidden"


class BudgetExhausted(FixloopError):
    """The next step would break a cap. Raised *before* anything is spent."""

    code = "exhausted_budget"

    def __init__(self, dimension: str, message: str = "", **details: Any) -> None:
        super().__init__(message or f"budget exhausted: {dimension}", dimension=dimension, **details)
        self.dimension = dimension


class EnvFailed(FixloopError):
    """Dependencies could not be installed, or the tests need network or services."""

    code = "env_failed"


class FlakyOracle(FixloopError):
    code = "flaky_oracle"


class PolicyViolationError(FixloopError):
    code = "policy_violation"


class InfraError(FixloopError):
    """A platform failure (runner died, object store down). Usually retried before surfacing."""

    code = "infra_error"

    def __init__(self, message: str = "", retryable: bool = True, **details: Any) -> None:
        super().__init__(message, **details)
        self.retryable = retryable


class TaskCancelled(FixloopError):
    code = "cancelled"


class LeaseLost(FixloopError):
    """This worker no longer owns the task (its fence is stale). Stop without writing."""

    code = "lease_lost"


class ProviderError(FixloopError):
    """A model provider error. `transient` errors are retried with backoff."""

    code = "provider_error"

    def __init__(
        self,
        message: str = "",
        status: int | None = None,
        transient: bool = True,
        retry_after: float | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message, status=status, **details)
        self.status = status
        self.transient = transient
        self.retry_after = retry_after


class ProviderRefusal(FixloopError):
    """The provider refused to answer (safety, recitation and similar finish reasons)."""

    code = "provider_refusal"


class FormatError(FixloopError):
    """The model's output failed schema validation even after the allowed re-asks."""

    code = "format_error"


class CircuitOpen(FixloopError):
    code = "circuit_open"
