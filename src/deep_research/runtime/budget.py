"""Deterministic, session-scoped runtime budget accounting."""

from __future__ import annotations

from collections.abc import Callable

from deep_research.runtime.contracts import (
    BudgetUsage,
    FailureKind,
    OperationKind,
    RuntimeControlError,
    RuntimeLimits,
)


class RuntimeBudgetManager:
    """Enforces independent iteration, request, token, and wall-clock budgets."""

    def __init__(
        self,
        limits: RuntimeLimits,
        monotonic: Callable[[], float],
    ) -> None:
        self.limits = limits
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._usage = BudgetUsage()

    def reset(self) -> None:
        self._started_at = self._monotonic()
        self._usage = BudgetUsage()

    def begin_iteration(self) -> None:
        self._check_emergency_stop()
        self.check_time()
        if self._usage.iterations_started >= self.limits.max_research_iterations:
            self._raise_budget("Research iteration budget exhausted")
        self._usage.iterations_started += 1
        self._usage.tool_calls_this_iteration = 0
        self._usage.model_calls_this_iteration = 0

    def before_call(
        self,
        kind: OperationKind,
        *,
        operation: str,
        external_api: bool = False,
        requested_tokens: int = 0,
    ) -> None:
        self._check_emergency_stop()
        self.check_time()
        if requested_tokens < 0:
            raise ValueError("requested_tokens cannot be negative")
        if requested_tokens > self.limits.max_tokens_per_call:
            self._raise_budget(
                "Requested model tokens exceed the per-call budget",
                operation=operation,
                operation_kind=kind,
            )

        if kind is OperationKind.TOOL:
            if (
                self._usage.tool_calls_this_iteration
                >= self.limits.max_tool_calls_per_iteration
            ):
                self._raise_budget(
                    "Tool-call iteration budget exhausted",
                    operation=operation,
                    operation_kind=kind,
                )
            if self._usage.tool_calls >= self.limits.max_tool_calls_total:
                self._raise_budget(
                    "Tool-call session budget exhausted",
                    operation=operation,
                    operation_kind=kind,
                )
            if external_api and (
                self._usage.external_api_calls >= self.limits.max_external_api_calls
            ):
                self._raise_budget(
                    "External API-call budget exhausted",
                    operation=operation,
                    operation_kind=kind,
                )
            self._usage.tool_calls += 1
            self._usage.tool_calls_this_iteration += 1
            if external_api:
                self._usage.external_api_calls += 1
        else:
            if (
                self._usage.model_calls_this_iteration
                >= self.limits.max_model_calls_per_iteration
            ):
                self._raise_budget(
                    "Model-call iteration budget exhausted",
                    operation=operation,
                    operation_kind=kind,
                )
            if self._usage.model_calls >= self.limits.max_model_calls_total:
                self._raise_budget(
                    "Model-call session budget exhausted",
                    operation=operation,
                    operation_kind=kind,
                )
            self._usage.model_calls += 1
            self._usage.model_calls_this_iteration += 1

    def record_tokens(self, tokens: int, *, operation: str) -> None:
        if tokens < 0:
            raise ValueError("tokens cannot be negative")
        if tokens > self.limits.max_tokens_per_call:
            self._raise_budget(
                "Model response exceeded the per-call token budget",
                operation=operation,
                operation_kind=OperationKind.MODEL,
            )
        if self._usage.tokens + tokens > self.limits.max_tokens_total:
            self._raise_budget(
                "Model token session budget exhausted",
                operation=operation,
                operation_kind=OperationKind.MODEL,
            )
        self._usage.tokens += tokens

    def remaining_seconds(self) -> float:
        elapsed = max(0.0, self._monotonic() - self._started_at)
        return max(0.0, self.limits.max_research_time_seconds - elapsed)

    def check_time(self) -> None:
        if self.remaining_seconds() <= 0:
            self._raise_budget("Research wall-clock budget exhausted")

    def snapshot(self) -> BudgetUsage:
        usage = self._usage.model_copy(deep=True)
        usage.elapsed_seconds = max(0.0, self._monotonic() - self._started_at)
        return usage

    def _check_emergency_stop(self) -> None:
        if self.limits.emergency_stop:
            raise RuntimeControlError(
                "Research execution halted by emergency stop",
                failure_kind=FailureKind.EMERGENCY_STOP,
            )

    @staticmethod
    def _raise_budget(
        message: str,
        *,
        operation: str | None = None,
        operation_kind: OperationKind | None = None,
    ) -> None:
        raise RuntimeControlError(
            message,
            failure_kind=FailureKind.BUDGET,
            operation=operation,
            operation_kind=operation_kind,
        )
