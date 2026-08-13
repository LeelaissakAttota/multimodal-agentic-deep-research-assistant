"""Phase 7 end-to-end, adversarial, security, and bounded-batch tests."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Never
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.context.context_builder import ContextPolicy, ResearchContextBuilder
from deep_research.core.agents.analysis_agent import DeterministicAnalysisAgent
from deep_research.core.agents.deterministic_research_agent import (
    DeterministicResearchAgent,
    FakeWebSearchTool,
)
from deep_research.core.agents.evaluation_agent import DeterministicEvaluationAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent
from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.core.config import Settings
from deep_research.core.orchestration.master_research_orchestrator import (
    MasterResearchOrchestrator,
)
from deep_research.demo import MAX_DEMO_SCENARIOS, run_demo_scenarios
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.memory.research_memory import BoundedResearchMemory
from deep_research.persistence.sqlite_repository import SQLiteResearchRepository
from deep_research.runtime.contracts import RuntimeLimits
from deep_research.runtime.harness import ExecutionHarness, InMemoryRuntimeObserver
from deep_research.security import find_sensitive_data_path
from deep_research.tools.tool import ToolRequest


@pytest.fixture
def phase7_database() -> Iterator[Path]:
    database_path = Path("data") / "phase7-tests" / f"{uuid4()}.sqlite3"
    yield database_path
    for candidate in (
        database_path,
        database_path.with_name(f"{database_path.name}-shm"),
        database_path.with_name(f"{database_path.name}-wal"),
    ):
        candidate.unlink(missing_ok=True)


def _full_stack(database_path: Path) -> MasterResearchOrchestrator:
    repository = SQLiteResearchRepository(database_path)
    context_builder = ResearchContextBuilder(
        BoundedResearchMemory(repository),
        ContextPolicy(max_characters=300, max_evidence=3, max_reflections=2),
    )
    harness = ExecutionHarness(
        RuntimeLimits(
            max_research_iterations=2,
            max_tool_calls_per_iteration=5,
            max_tool_calls_total=10,
            max_model_calls_per_iteration=8,
            max_model_calls_total=16,
            max_tokens_per_call=100,
            max_tokens_total=1_000,
            max_research_time_seconds=10,
            max_tool_call_time_seconds=1,
            max_model_call_time_seconds=1,
            max_external_api_calls=0,
            max_tool_retry_attempts=0,
            max_model_retry_attempts=0,
            retry_backoff_seconds=0,
            retry_backoff_max_seconds=0,
        ),
        observer=InMemoryRuntimeObserver(max_events=100),
    )
    return MasterResearchOrchestrator(
        planning_agent=DeterministicPlanningAgent(),
        research_agent=DeterministicResearchAgent(),
        analysis_agent=DeterministicAnalysisAgent(),
        evaluation_agent=DeterministicEvaluationAgent(),
        report_agent=DeterministicReportAgent(),
        max_iterations=2,
        session_repository=repository,
        context_builder=context_builder,
        execution_harness=harness,
    )


@pytest.mark.asyncio
async def test_full_stack_offline_run_preserves_bounds_provenance_and_recovery(
    phase7_database: Path,
) -> None:
    orchestrator = _full_stack(phase7_database)
    request = ResearchRequest(
        id=UUID(int=7001),
        objective="Evaluate deterministic grid storage reliability evidence",
    )

    state = await orchestrator.start_research(request)
    report = state.metadata["report"]
    runtime = state.metadata["runtime"]
    repository = orchestrator.session_repository
    assert isinstance(repository, SQLiteResearchRepository)
    recovered = _full_stack(phase7_database).recover_session(state.id)
    persisted = repository.load_session(state.id)

    assert state.status == "completed"
    assert state.status_history == [
        "initialized",
        "planning",
        "researching",
        "analyzing",
        "evaluating",
        "reporting",
        "completed",
    ]
    assert report["evidence_gathered"] == [
        str(evidence_id) for evidence_id in state.gathered_evidence
    ]
    assert report["sources_consulted"] == [
        str(source_id) for source_id in state.consulted_sources
    ]
    assert runtime["usage"]["iterations_started"] == 1
    assert runtime["usage"]["tool_calls"] == 3
    assert runtime["usage"]["model_calls"] == 4
    assert runtime["usage"]["external_api_calls"] == 0
    assert runtime["failure_count"] == 0
    assert recovered == state
    assert persisted.state == state
    assert len(persisted.plans) == 1
    assert all(task.status == "completed" for task in persisted.plans[0].tasks)
    assert orchestrator.working_context is not None
    assert orchestrator.working_context.text_character_count <= 300


@pytest.mark.asyncio
async def test_product_registry_and_demo_batch_remain_bounded() -> None:
    application = ResearchApplication(Settings(), max_sessions=2)
    first = await application.submit(ResearchSubmission(objective="first objective"))
    second = await application.submit(ResearchSubmission(objective="second objective"))
    third = await application.submit(ResearchSubmission(objective="third objective"))

    assert application.get(first.session_id) is None
    assert application.get(second.session_id) is not None
    assert application.get(third.session_id) is not None

    demos = await run_demo_scenarios(["bounded scenario one", "bounded scenario two"])
    assert [result.status for result in demos] == ["completed", "completed"]
    assert all(result.runtime is not None for result in demos)
    with pytest.raises(ValueError, match="cannot exceed"):
        await run_demo_scenarios(
            [f"scenario {index}" for index in range(MAX_DEMO_SCENARIOS + 1)]
        )
    with pytest.raises(ValueError, match="At least one"):
        await run_demo_scenarios([])


def test_submission_rejects_adversarial_or_unbounded_metadata() -> None:
    with pytest.raises(ValidationError, match="secret-like metadata"):
        ResearchSubmission(
            objective="safe objective",
            metadata={"authorization": "Bearer must-not-be-accepted"},
        )
    with pytest.raises(ValidationError, match="null characters"):
        ResearchSubmission(objective="unsafe\x00objective")
    with pytest.raises(ValidationError, match="at most 50 items"):
        ResearchSubmission(
            objective="safe objective",
            metadata={f"key-{index}": index for index in range(51)},
        )
    with pytest.raises(ValidationError, match="cannot exceed 2000"):
        ResearchSubmission(
            objective="safe objective",
            metadata={"oversized": "x" * 2_001},
        )
    with pytest.raises(ValidationError, match="must be finite"):
        ResearchSubmission(
            objective="safe objective",
            metadata={"score": float("inf")},
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ResearchSubmission.model_validate(
            {"objective": "safe objective", "unexpected": "field"}
        )


def test_sensitive_data_detection_never_returns_the_secret_value() -> None:
    secret = "token-value-that-must-not-appear"
    secret_path = find_sensitive_data_path(
        {"nested": [{"provider_token": secret}]},
        "snapshot",
    )
    credential_url_path = find_sensitive_data_path(
        {"source": "https://user:password@example.com/resource"},
        "snapshot",
    )

    assert secret_path == "snapshot.nested[0].provider_token"
    assert secret not in secret_path
    assert credential_url_path == "snapshot.source"


@pytest.mark.asyncio
async def test_tool_exception_is_normalized_without_leaking_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "Bearer phase7-secret"

    class ExplodingTool(FakeWebSearchTool):
        async def execute(self, request: ToolRequest) -> Never:
            raise RuntimeError(secret)

    agent = DeterministicResearchAgent()
    monkeypatch.setattr(agent.registry, "get", lambda identifier: ExplodingTool)
    task = ResearchTask(
        plan_id=UUID(int=7100),
        description="Exercise sanitized failure handling",
        objective="failure handling",
        assigned_tool="web_search",
        tool_input={"query": "failure handling"},
    )

    result = await agent.execute_task(task)

    assert result.success is False
    assert result.error == "Tool execution failed"
    assert secret not in str(result.model_dump())
    assert result.metadata == {
        "exception_type": "RuntimeError",
        "failure_kind": "permanent",
        "retryable": False,
    }
