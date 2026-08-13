"""Provider-neutral runtime reliability contracts for Phase 6."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from deep_research.errors.base import DeepResearchError


class OperationKind(str, Enum):
    """Kinds of metered external execution."""

    TOOL = "tool"
    MODEL = "model"


class FailureKind(str, Enum):
    """Normalized terminal failure categories."""

    TRANSIENT = "transient"
    PERMANENT = "permanent"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    CONFIGURATION = "configuration"
    EMERGENCY_STOP = "emergency_stop"


class RuntimeLimits(BaseModel):
    """Validated session, call, token, and retry limits."""

    max_research_iterations: int = Field(default=3, ge=1)
    max_tool_calls_per_iteration: int = Field(default=20, ge=1)
    max_tool_calls_total: int = Field(default=60, ge=1)
    max_model_calls_per_iteration: int = Field(default=15, ge=1)
    max_model_calls_total: int = Field(default=45, ge=1)
    max_tokens_per_call: int = Field(default=4_000, ge=1)
    max_tokens_total: int = Field(default=50_000, ge=1)
    max_research_time_seconds: float = Field(default=300.0, gt=0)
    max_tool_call_time_seconds: float = Field(default=30.0, gt=0)
    max_model_call_time_seconds: float = Field(default=60.0, gt=0)
    max_external_api_calls: int = Field(default=10, ge=0)
    max_tool_retry_attempts: int = Field(default=2, ge=0)
    max_model_retry_attempts: int = Field(default=2, ge=0)
    retry_backoff_seconds: float = Field(default=0.1, ge=0)
    retry_backoff_max_seconds: float = Field(default=2.0, ge=0)
    emergency_stop: bool = False

    @model_validator(mode="after")
    def validate_totals(self) -> RuntimeLimits:
        if self.retry_backoff_max_seconds < self.retry_backoff_seconds:
            raise ValueError(
                "retry_backoff_max_seconds must be at least retry_backoff_seconds"
            )
        return self


class RetryPolicy(BaseModel):
    """Explicit bounded retry policy for one operation kind."""

    max_retries: int = Field(ge=0)
    timeout_seconds: float = Field(gt=0)
    backoff_seconds: float = Field(default=0.1, ge=0)
    max_backoff_seconds: float = Field(default=2.0, ge=0)

    @model_validator(mode="after")
    def validate_backoff(self) -> RetryPolicy:
        if self.max_backoff_seconds < self.backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least backoff_seconds")
        return self

    def delay_before_attempt(self, attempt: int) -> float:
        """Return deterministic exponential delay before a retry attempt."""
        if attempt <= 1:
            return 0.0
        return float(
            min(
                self.backoff_seconds * (2 ** (attempt - 2)),
                self.max_backoff_seconds,
            )
        )


class BudgetUsage(BaseModel):
    """Serializable resource usage for a research session."""

    iterations_started: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    external_api_calls: int = Field(default=0, ge=0)
    tokens: int = Field(default=0, ge=0)
    elapsed_seconds: float = Field(default=0.0, ge=0)
    tool_calls_this_iteration: int = Field(default=0, ge=0)
    model_calls_this_iteration: int = Field(default=0, ge=0)


class RuntimeEvent(BaseModel):
    """Sanitized structured event emitted by the execution harness."""

    event: str
    operation: str | None = None
    operation_kind: OperationKind | None = None
    attempt: int | None = Field(default=None, ge=1)
    failure_kind: FailureKind | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RuntimeReport(BaseModel):
    """Terminal, persistence-safe execution report."""

    usage: BudgetUsage
    event_count: int = Field(default=0, ge=0)
    failure_count: int = Field(default=0, ge=0)
    last_failure: dict[str, Any] | None = None


@runtime_checkable
class RuntimeObserver(Protocol):
    """Observability sink boundary; infrastructure implementations stay optional."""

    def record(self, event: RuntimeEvent) -> None:
        """Record a sanitized runtime event."""


class RuntimeControlError(DeepResearchError):
    """Normalized bounded execution failure."""

    def __init__(
        self,
        message: str,
        *,
        failure_kind: FailureKind,
        operation: str | None = None,
        operation_kind: OperationKind | None = None,
        attempts: int = 0,
        details: dict[str, Any] | None = None,
    ) -> None:
        normalized_details = dict(details or {})
        normalized_details.update(
            {
                "failure_kind": failure_kind.value,
                "operation": operation,
                "operation_kind": operation_kind.value if operation_kind else None,
                "attempts": attempts,
            }
        )
        super().__init__(
            message,
            error_code=f"runtime_{failure_kind.value}",
            details=normalized_details,
        )
        self.failure_kind = failure_kind
        self.operation = operation
        self.operation_kind = operation_kind
        self.attempts = attempts


class TransientExecutionError(Exception):
    """Provider/tool signal that an operation may be retried."""


class PermanentExecutionError(Exception):
    """Provider/tool signal that retrying cannot make progress."""
