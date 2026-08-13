"""Phase 6 bounded runtime, routing, recovery, and observability tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from deep_research.context.context_builder import ContextPolicy, ResearchContextBuilder
from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.planning_agent import PlanningAgent
from deep_research.core.agents.report_agent import ReportAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.orchestration.master_research_orchestrator import (
    MasterResearchOrchestrator,
)
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.errors.base import ModelError
from deep_research.memory.research_memory import BoundedResearchMemory
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.models.routed_gateway import ModelRoute, RoutedModelGateway
from deep_research.persistence.sqlite_repository import SQLiteResearchRepository
from deep_research.runtime import (
    ExecutionHarness,
    FailureKind,
    PermanentExecutionError,
    RuntimeControlError,
    RuntimeLimits,
    TransientExecutionError,
)
from deep_research.runtime.harness import InMemoryRuntimeObserver
from deep_research.tools.tool import ToolResult


def limits(**overrides: object) -> RuntimeLimits:
    values: dict[str, object] = {
        "max_research_iterations": 2,
        "max_tool_calls_per_iteration": 5,
        "max_tool_calls_total": 10,
        "max_model_calls_per_iteration": 8,
        "max_model_calls_total": 16,
        "max_tokens_per_call": 20,
        "max_tokens_total": 40,
        "max_research_time_seconds": 10.0,
        "max_tool_call_time_seconds": 0.05,
        "max_model_call_time_seconds": 0.05,
        "max_external_api_calls": 2,
        "max_tool_retry_attempts": 2,
        "max_model_retry_attempts": 1,
        "retry_backoff_seconds": 0.1,
        "retry_backoff_max_seconds": 0.2,
    }
    values.update(overrides)
    return RuntimeLimits.model_validate(values)


@pytest.fixture
def phase6_database() -> Iterator[Path]:
    database_path = Path("data") / "phase6-tests" / f"{uuid4()}.sqlite3"
    yield database_path
    if database_path.exists():
        database_path.unlink()


@pytest.mark.asyncio
async def test_transient_failure_retries_with_deterministic_backoff() -> None:
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)

    harness = ExecutionHarness(limits(), sleeper=sleeper)
    harness.start_session()
    harness.begin_iteration()
    attempts = 0

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransientExecutionError("provider unavailable")
        return "ok"

    result = await harness.execute_tool("search", operation)

    assert result == "ok"
    assert attempts == 2
    assert delays == [0.1]
    assert harness.report().usage.tool_calls == 2
    assert harness.report().failure_count == 0


@pytest.mark.asyncio
async def test_normalized_transient_tool_result_is_retried() -> None:
    harness = ExecutionHarness(limits(), sleeper=AsyncMock())
    harness.start_session()
    harness.begin_iteration()
    operation = AsyncMock(
        side_effect=[
            ToolResult(
                success=False,
                error="temporary",
                tool_name="search",
                metadata={"failure_kind": "transient", "retryable": True},
            ),
            ToolResult(success=True, output="ok", tool_name="search"),
        ]
    )

    result = await harness.execute_tool("search", operation)

    assert result.success is True
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_retry_exhaustion_is_bounded_and_observable() -> None:
    observer = InMemoryRuntimeObserver()
    harness = ExecutionHarness(limits(), observer=observer, sleeper=AsyncMock())
    harness.start_session()
    harness.begin_iteration()
    operation = AsyncMock(side_effect=TransientExecutionError("still unavailable"))

    with pytest.raises(RuntimeControlError) as caught:
        await harness.execute_tool("search", operation)

    assert caught.value.failure_kind is FailureKind.TRANSIENT
    assert caught.value.attempts == 3
    assert operation.await_count == 3
    assert harness.report().failure_count == 1
    assert observer.events[-1].event == "operation_terminal_failure"
    assert all("still unavailable" not in str(event.metadata) for event in observer.events)


@pytest.mark.asyncio
async def test_permanent_failure_does_not_retry() -> None:
    harness = ExecutionHarness(limits(), sleeper=AsyncMock())
    harness.start_session()
    harness.begin_iteration()
    operation = AsyncMock(side_effect=PermanentExecutionError("invalid request"))

    with pytest.raises(RuntimeControlError) as caught:
        await harness.execute_model("analysis", operation)

    assert caught.value.failure_kind is FailureKind.PERMANENT
    assert caught.value.attempts == 1
    assert operation.await_count == 1


@pytest.mark.asyncio
async def test_timeout_is_normalized_and_bounded() -> None:
    harness = ExecutionHarness(
        limits(max_tool_retry_attempts=0, max_tool_call_time_seconds=0.001)
    )
    harness.start_session()
    harness.begin_iteration()

    async def slow_operation() -> None:
        await asyncio.sleep(0.1)

    with pytest.raises(RuntimeControlError) as caught:
        await harness.execute_tool("slow_tool", slow_operation)

    assert caught.value.failure_kind is FailureKind.TIMEOUT
    assert caught.value.attempts == 1


@pytest.mark.asyncio
async def test_request_and_external_api_budgets_stop_before_extra_call() -> None:
    harness = ExecutionHarness(
        limits(
            max_tool_calls_per_iteration=1,
            max_tool_calls_total=1,
            max_external_api_calls=1,
            max_tool_retry_attempts=0,
        )
    )
    harness.start_session()
    harness.begin_iteration()
    operation = AsyncMock(return_value="ok")

    assert await harness.execute_tool("paid_search", operation, external_api=True) == "ok"
    with pytest.raises(RuntimeControlError) as caught:
        await harness.execute_tool("paid_search", operation, external_api=True)

    assert caught.value.failure_kind is FailureKind.BUDGET
    assert operation.await_count == 1


@pytest.mark.asyncio
async def test_token_budget_and_emergency_stop_are_fail_closed() -> None:
    token_harness = ExecutionHarness(limits(max_model_retry_attempts=0))
    token_harness.start_session()
    token_harness.begin_iteration()

    async def oversized_response() -> ModelResponse:
        return ModelResponse(
            text="result",
            model_id="fake",
            usage={"total_tokens": 21},
        )

    with pytest.raises(RuntimeControlError) as token_error:
        await token_harness.execute_model("provider", oversized_response)
    assert token_error.value.failure_kind is FailureKind.BUDGET

    stopped = ExecutionHarness(limits(emergency_stop=True))
    stopped.start_session()
    with pytest.raises(RuntimeControlError) as stop_error:
        stopped.begin_iteration()
    assert stop_error.value.failure_kind is FailureKind.EMERGENCY_STOP


def test_invalid_runtime_configuration_is_rejected() -> None:
    with pytest.raises(ValidationError, match="max_tool_calls_total"):
        RuntimeLimits(max_tool_calls_total=0)
    with pytest.raises(ValidationError, match="retry_backoff_max_seconds"):
        RuntimeLimits(
            retry_backoff_seconds=2,
            retry_backoff_max_seconds=1,
        )


@pytest.mark.asyncio
async def test_wall_clock_budget_stops_before_execution() -> None:
    current_time = [0.0]
    harness = ExecutionHarness(
        limits(max_research_time_seconds=1.0),
        monotonic=lambda: current_time[0],
    )
    harness.start_session()
    harness.begin_iteration()
    current_time[0] = 1.0
    operation = AsyncMock(return_value="unused")

    with pytest.raises(RuntimeControlError) as caught:
        await harness.execute_tool("search", operation)

    assert caught.value.failure_kind is FailureKind.BUDGET
    operation.assert_not_awaited()


class FakeGateway(ModelGateway):
    def __init__(self, response: ModelResponse | None = None, failure: Exception | None = None):
        self.response = response
        self.failure = failure
        self.calls = 0

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        assert self.response is not None
        return self.response

    async def health_check(self) -> bool:
        return self.failure is None


@pytest.mark.asyncio
async def test_model_router_falls_back_after_bounded_transient_failure() -> None:
    harness = ExecutionHarness(limits(), sleeper=AsyncMock())
    harness.start_session()
    harness.begin_iteration()
    primary = FakeGateway(failure=TransientExecutionError("temporary"))
    fallback = FakeGateway(ModelResponse(text="ok", model_id="fallback"))
    router = RoutedModelGateway(
        [ModelRoute("primary", primary), ModelRoute("fallback", fallback)],
        harness,
    )

    response = await router.generate(ModelRequest(prompt="research", parameters={"max_tokens": 10}))

    assert response.model_id == "fallback"
    assert primary.calls == 2
    assert fallback.calls == 1
    observer = harness.observer
    assert isinstance(observer, InMemoryRuntimeObserver)
    assert any(event.event == "model_fallback" for event in observer.events)


@pytest.mark.asyncio
async def test_model_router_does_not_fallback_on_permanent_failure() -> None:
    harness = ExecutionHarness(limits(), sleeper=AsyncMock())
    harness.start_session()
    harness.begin_iteration()
    primary = FakeGateway(failure=PermanentExecutionError("bad input"))
    fallback = FakeGateway(ModelResponse(text="unused", model_id="fallback"))
    router = RoutedModelGateway(
        [ModelRoute("primary", primary), ModelRoute("fallback", fallback)],
        harness,
    )

    with pytest.raises(ModelError, match="All eligible model routes failed"):
        await router.generate(ModelRequest(prompt="research"))
    assert primary.calls == 1
    assert fallback.calls == 0


def make_reliable_orchestrator(
    database_path: Path,
) -> tuple[MasterResearchOrchestrator, ResearchRequest]:
    request = ResearchRequest(id=UUID(int=5001), objective="Reliable storage research")
    evidence_id = UUID(int=5004)
    source_id = UUID(int=5005)
    task = ResearchTask(
        id=UUID(int=5003),
        plan_id=UUID(int=5002),
        description="Gather storage evidence",
        objective="storage evidence",
    )
    plan = ResearchPlan(
        id=task.plan_id,
        request_id=request.id,
        objective=request.objective,
        tasks=[task],
    )
    planning = MagicMock(spec=PlanningAgent)
    planning.create_plan = AsyncMock(
        side_effect=[TransientExecutionError("temporary"), plan]
    )
    research = MagicMock(spec=ResearchAgent)
    research.execute_task = AsyncMock(
        return_value=ToolResult(
            success=True,
            output={
                "summary": "evidence",
                "evidence_ids": [evidence_id],
                "source_ids": [source_id],
            },
            tool_name="web_search",
        )
    )
    analysis = MagicMock(spec=AnalysisAgent)
    analysis.analyze = AsyncMock(return_value={"summary": "analysis"})
    evaluation = MagicMock(spec=EvaluationAgent)
    evaluation.evaluate = AsyncMock(
        return_value=EvaluationResult(decision="COMPLETE", confidence=0.9)
    )
    report = MagicMock(spec=ReportAgent)
    report.generate_report = AsyncMock(return_value={"content": "report"})
    repository = SQLiteResearchRepository(database_path)
    context_builder = ResearchContextBuilder(
        BoundedResearchMemory(repository),
        ContextPolicy(max_characters=200, max_evidence=2),
    )
    harness = ExecutionHarness(limits(), sleeper=AsyncMock())
    orchestrator = MasterResearchOrchestrator(
        planning,
        research,
        analysis,
        evaluation,
        report,
        max_iterations=2,
        session_repository=repository,
        context_builder=context_builder,
        execution_harness=harness,
    )
    return orchestrator, request


@pytest.mark.asyncio
async def test_orchestrator_retries_without_consuming_research_iteration_and_recovers(
    phase6_database: Path,
) -> None:
    orchestrator, request = make_reliable_orchestrator(phase6_database)

    state = await orchestrator.start_research(request)
    recovered_orchestrator, _ = make_reliable_orchestrator(phase6_database)
    recovered = recovered_orchestrator.recover_session(state.id)

    assert state.status == "completed"
    assert state.iteration_number == 0
    assert orchestrator.planning_agent.create_plan.await_count == 2
    assert state.gathered_evidence == [UUID(int=5004)]
    assert state.consulted_sources == [UUID(int=5005)]
    assert state.metadata["runtime"]["usage"]["iterations_started"] == 1
    assert state.metadata["runtime"]["usage"]["model_calls"] == 5
    assert recovered == state
    assert orchestrator.working_context is not None
    assert orchestrator.working_context.text_character_count <= 200


@pytest.mark.asyncio
async def test_orchestrator_runtime_exhaustion_reaches_traceable_terminal_state(
    phase6_database: Path,
) -> None:
    orchestrator, request = make_reliable_orchestrator(phase6_database)
    orchestrator.planning_agent.create_plan.side_effect = PermanentExecutionError("invalid")

    state = await orchestrator.start_research(request)

    assert state.status == "failed"
    assert state.status_history == ["initialized", "planning", "failed"]
    assert state.metadata["runtime_failure"]["failure_kind"] == "permanent"
    assert state.metadata["runtime"]["failure_count"] == 1
    recovered = orchestrator.session_repository.load_session(state.id)  # type: ignore[union-attr]
    assert recovered.state.metadata["runtime_failure"] == state.metadata["runtime_failure"]
