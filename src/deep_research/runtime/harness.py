"""Bounded retry, timeout, budget, and observability execution harness."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from deep_research.runtime.budget import RuntimeBudgetManager
from deep_research.runtime.contracts import (
    FailureKind,
    OperationKind,
    PermanentExecutionError,
    RetryPolicy,
    RuntimeControlError,
    RuntimeEvent,
    RuntimeLimits,
    RuntimeObserver,
    RuntimeReport,
    TransientExecutionError,
)

T = TypeVar("T")


@dataclass
class _AttemptFailure(Exception):
    failure_kind: FailureKind
    retryable: bool
    cause: BaseException


class InMemoryRuntimeObserver:
    """Bounded in-memory event sink suitable for reports and deterministic tests."""

    def __init__(self, max_events: int = 1_000) -> None:
        if max_events < 1:
            raise ValueError("max_events must be at least 1")
        self.max_events = max_events
        self.events: list[RuntimeEvent] = []

    def record(self, event: RuntimeEvent) -> None:
        if len(self.events) < self.max_events:
            self.events.append(event.model_copy(deep=True))


class ExecutionHarness:
    """Session-scoped execution boundary with no unbounded retry path."""

    def __init__(
        self,
        limits: RuntimeLimits | None = None,
        *,
        observer: RuntimeObserver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.limits = limits or RuntimeLimits()
        self.observer = observer or InMemoryRuntimeObserver()
        self._sleeper = sleeper
        self.budget = RuntimeBudgetManager(self.limits, monotonic)
        self._last_failure: dict[str, Any] | None = None
        self._failure_count = 0

    def start_session(self) -> None:
        self.budget.reset()
        self._last_failure = None
        self._failure_count = 0
        if isinstance(self.observer, InMemoryRuntimeObserver):
            self.observer.events.clear()
        self.emit("session_started")

    def begin_iteration(self) -> None:
        try:
            self.budget.begin_iteration()
        except RuntimeControlError as exc:
            self._record_terminal_failure(exc)
            raise
        self.emit(
            "iteration_started",
            metadata={"iteration": self.budget.snapshot().iterations_started - 1},
        )

    async def execute_tool(
        self,
        operation: str,
        call: Callable[[], Awaitable[T]],
        *,
        external_api: bool = False,
    ) -> T:
        policy = RetryPolicy(
            max_retries=self.limits.max_tool_retry_attempts,
            timeout_seconds=self.limits.max_tool_call_time_seconds,
            backoff_seconds=self.limits.retry_backoff_seconds,
            max_backoff_seconds=self.limits.retry_backoff_max_seconds,
        )
        return await self._execute(
            OperationKind.TOOL,
            operation,
            call,
            policy,
            external_api=external_api,
        )

    async def execute_model(
        self,
        operation: str,
        call: Callable[[], Awaitable[T]],
        *,
        requested_tokens: int = 0,
    ) -> T:
        policy = RetryPolicy(
            max_retries=self.limits.max_model_retry_attempts,
            timeout_seconds=self.limits.max_model_call_time_seconds,
            backoff_seconds=self.limits.retry_backoff_seconds,
            max_backoff_seconds=self.limits.retry_backoff_max_seconds,
        )
        result = await self._execute(
            OperationKind.MODEL,
            operation,
            call,
            policy,
            requested_tokens=requested_tokens,
        )
        usage = getattr(result, "usage", None)
        if isinstance(usage, dict):
            raw_tokens = usage.get("total_tokens", 0)
            if isinstance(raw_tokens, int):
                try:
                    self.budget.record_tokens(raw_tokens, operation=operation)
                except RuntimeControlError as exc:
                    self._record_terminal_failure(exc)
                    raise
        return result

    def emit(
        self,
        event: str,
        *,
        operation: str | None = None,
        operation_kind: OperationKind | None = None,
        attempt: int | None = None,
        failure_kind: FailureKind | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.observer.record(
            RuntimeEvent(
                event=event,
                operation=operation,
                operation_kind=operation_kind,
                attempt=attempt,
                failure_kind=failure_kind,
                metadata=dict(metadata or {}),
            )
        )

    def report(self) -> RuntimeReport:
        events = (
            self.observer.events
            if isinstance(self.observer, InMemoryRuntimeObserver)
            else []
        )
        return RuntimeReport(
            usage=self.budget.snapshot(),
            event_count=len(events),
            failure_count=self._failure_count,
            last_failure=dict(self._last_failure) if self._last_failure else None,
        )

    async def _execute(
        self,
        kind: OperationKind,
        operation: str,
        call: Callable[[], Awaitable[T]],
        policy: RetryPolicy,
        *,
        external_api: bool = False,
        requested_tokens: int = 0,
    ) -> T:
        max_attempts = policy.max_retries + 1
        for attempt in range(1, max_attempts + 1):
            delay = policy.delay_before_attempt(attempt)
            if delay:
                delay = min(delay, self.budget.remaining_seconds())
                self.emit(
                    "operation_backoff",
                    operation=operation,
                    operation_kind=kind,
                    attempt=attempt,
                    metadata={"delay_seconds": delay},
                )
                await self._sleeper(delay)
            try:
                self.budget.before_call(
                    kind,
                    operation=operation,
                    external_api=external_api,
                    requested_tokens=requested_tokens,
                )
            except RuntimeControlError as exc:
                self._record_terminal_failure(exc)
                raise

            self.emit(
                "operation_started",
                operation=operation,
                operation_kind=kind,
                attempt=attempt,
            )
            timeout = min(policy.timeout_seconds, self.budget.remaining_seconds())
            if timeout <= 0:
                error = RuntimeControlError(
                    "Research wall-clock budget exhausted",
                    failure_kind=FailureKind.BUDGET,
                    operation=operation,
                    operation_kind=kind,
                    attempts=attempt - 1,
                )
                self._record_terminal_failure(error)
                raise error
            try:
                result = await self._execute_once(call, timeout)
            except _AttemptFailure as failure:
                failure_kind = failure.failure_kind
                retryable = failure.retryable
                cause = failure.cause
            else:
                result_failure = self._retryable_result_failure(kind, result)
                if result_failure is None:
                    if (
                        kind is OperationKind.TOOL
                        and getattr(result, "success", None) is False
                    ):
                        self.emit(
                            "operation_completed_with_failure",
                            operation=operation,
                            operation_kind=kind,
                            attempt=attempt,
                            failure_kind=FailureKind.PERMANENT,
                        )
                        return result
                    self.emit(
                        "operation_succeeded",
                        operation=operation,
                        operation_kind=kind,
                        attempt=attempt,
                    )
                    return result
                failure_kind = result_failure.failure_kind
                retryable = result_failure.retryable
                cause = result_failure.cause

            self.emit(
                "operation_failed",
                operation=operation,
                operation_kind=kind,
                attempt=attempt,
                failure_kind=failure_kind,
                metadata={"exception_type": type(cause).__name__},
            )
            if retryable and attempt < max_attempts:
                self.emit(
                    "operation_retry_scheduled",
                    operation=operation,
                    operation_kind=kind,
                    attempt=attempt,
                    failure_kind=failure_kind,
                )
                continue

            error = RuntimeControlError(
                f"{kind.value.capitalize()} operation '{operation}' failed after "
                f"{attempt} attempt(s)",
                failure_kind=failure_kind,
                operation=operation,
                operation_kind=kind,
                attempts=attempt,
                details={"exception_type": type(cause).__name__},
            )
            self._record_terminal_failure(error)
            raise error from cause

        raise AssertionError("Bounded execution loop exited without a result")

    @staticmethod
    async def _execute_once(
        call: Callable[[], Awaitable[T]], timeout: float
    ) -> T:
        try:
            return await asyncio.wait_for(call(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise _AttemptFailure(FailureKind.TIMEOUT, True, exc) from exc
        except (TransientExecutionError, ConnectionError) as exc:
            raise _AttemptFailure(FailureKind.TRANSIENT, True, exc) from exc
        except PermanentExecutionError as exc:
            raise _AttemptFailure(FailureKind.PERMANENT, False, exc) from exc
        except Exception as exc:
            raise _AttemptFailure(FailureKind.PERMANENT, False, exc) from exc

    @staticmethod
    def _retryable_result_failure(
        kind: OperationKind, result: object
    ) -> _AttemptFailure | None:
        if (
            kind is not OperationKind.TOOL
            or getattr(result, "success", None) is not False
        ):
            return None
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        raw_kind = metadata.get("failure_kind")
        retryable = metadata.get("retryable") is True or raw_kind in {
            FailureKind.TRANSIENT.value,
            FailureKind.TIMEOUT.value,
        }
        if not retryable:
            return None
        failure_kind = (
            FailureKind.TIMEOUT
            if raw_kind == FailureKind.TIMEOUT.value
            else FailureKind.TRANSIENT
        )
        return _AttemptFailure(
            failure_kind,
            True,
            TransientExecutionError("Tool returned a retryable failure result"),
        )

    def _record_terminal_failure(self, error: RuntimeControlError) -> None:
        self._failure_count += 1
        self._last_failure = {
            "error_code": error.error_code,
            "message": error.message,
            "failure_kind": error.failure_kind.value,
            "operation": error.operation,
            "operation_kind": (
                error.operation_kind.value if error.operation_kind else None
            ),
            "attempts": error.attempts,
            "details": error.details,
        }
        self.emit(
            "operation_terminal_failure",
            operation=error.operation,
            operation_kind=error.operation_kind,
            failure_kind=error.failure_kind,
            metadata={
                "error_code": error.error_code,
                "attempts": error.attempts,
            },
        )
