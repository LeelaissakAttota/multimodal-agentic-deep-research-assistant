"""Phase 5 persistence, memory, context, and recovery tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

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
from deep_research.domain.research.research_state import EvaluationRecord, ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.evidence.claim import Claim
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.memory.research_memory import BoundedResearchMemory, MemoryQuery
from deep_research.persistence.contracts import (
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotFoundError,
    PersistenceSchemaError,
    ResearchSessionSnapshot,
    SecretDataError,
)
from deep_research.persistence.sqlite_repository import SQLiteResearchRepository
from deep_research.tools.tool import ToolResult

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def make_snapshot(
    *,
    session_id: UUID = UUID(int=1000),
    request_id: UUID = UUID(int=1001),
) -> ResearchSessionSnapshot:
    request = ResearchRequest(
        id=request_id,
        objective="Evaluate solar battery storage reliability",
        created_at=NOW,
    )
    task = ResearchTask(
        id=UUID(int=1003),
        plan_id=UUID(int=1002),
        description="Find storage reliability evidence",
        objective="solar battery storage reliability",
        status="completed",
        created_at=NOW,
        completed_at=NOW,
    )
    plan = ResearchPlan(
        id=task.plan_id,
        request_id=request.id,
        objective=request.objective,
        tasks=[task],
        created_at=NOW,
        updated_at=NOW,
        status="completed",
        progress=1.0,
    )
    solar_source = Source(
        id=UUID(int=1004),
        url="https://example.com/solar-storage",
        title="Solar storage field study",
        retrieved_at=NOW,
    )
    unrelated_source = Source(
        id=UUID(int=1005),
        url="https://example.com/quantum",
        title="Quantum networking overview",
        retrieved_at=NOW,
    )
    solar_evidence = Evidence(
        id=UUID(int=1006),
        source_id=solar_source.id,
        content="Solar battery storage reliability improved in the field study.",
        extracted_at=NOW,
    )
    unrelated_evidence = Evidence(
        id=UUID(int=1007),
        source_id=unrelated_source.id,
        content="Quantum networking uses entangled photons.",
        extracted_at=NOW,
    )
    claim = Claim(
        id=UUID(int=1008),
        text="Field evidence supports improved solar storage reliability.",
        supported_by=[solar_evidence.id],
        confidence=0.9,
        created_at=NOW,
    )
    state = ResearchState(
        id=session_id,
        request_id=request.id,
        current_plan_id=plan.id,
        plan_history=[plan.id],
        completed_task_ids=[task.id],
        consulted_sources=[solar_source.id, unrelated_source.id],
        gathered_evidence=[solar_evidence.id, unrelated_evidence.id],
        generated_claims=[claim.id],
        created_at=NOW,
        updated_at=NOW,
        status="completed",
        iteration_number=1,
        evaluation_result="COMPLETE",
        last_evaluation_reasoning="Evidence threshold met",
        last_evaluation_confidence=0.9,
        evaluation_history=[
            EvaluationRecord(
                iteration_number=0,
                decision="CONTINUE",
                confidence=0.6,
                reasoning="Need reliability evidence",
                gaps=["Long-term storage reliability"],
            ),
            EvaluationRecord(
                iteration_number=1,
                decision="COMPLETE",
                confidence=0.9,
                reasoning="Evidence threshold met",
                gaps=[],
            ),
        ],
        status_history=[
            "initialized",
            "planning",
            "researching",
            "analyzing",
            "evaluating",
            "reporting",
            "completed",
        ],
    )
    return ResearchSessionSnapshot(
        request=request,
        state=state,
        plans=[plan],
        sources=[solar_source, unrelated_source],
        evidence=[solar_evidence, unrelated_evidence],
        claims=[claim],
        evidence_task_links={solar_evidence.id: [task.id]},
        report_metadata={"title": "Storage reliability report"},
        saved_at=NOW,
    )


@pytest.fixture
def repository() -> Iterator[SQLiteResearchRepository]:
    database_path = Path("data") / "phase5-tests" / f"{uuid4()}.sqlite3"
    instance = SQLiteResearchRepository(database_path)
    yield instance
    if database_path.exists():
        database_path.unlink()


def test_sqlite_initialization_is_deterministic(repository):
    repository.initialize()
    repository.initialize()

    connection = sqlite3.connect(repository.database_path)
    try:
        version = connection.execute(
            "SELECT version FROM schema_metadata WHERE component = 'research_sessions'"
        ).fetchone()
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'research_sessions'"
        ).fetchone()
    finally:
        connection.close()

    assert version == (1,)
    assert table == ("research_sessions",)


def test_session_crud_and_round_trip_preserve_supported_state(repository):
    snapshot = make_snapshot()

    repository.save_session(snapshot)
    recovered = repository.load_session(snapshot.state.id)

    assert recovered == snapshot
    assert repository.list_sessions(limit=1) == [snapshot]
    assert recovered.state.evaluation_history == snapshot.state.evaluation_history
    assert recovered.evidence_task_links == snapshot.evidence_task_links
    assert recovered.claims[0].supported_by == [recovered.evidence[0].id]
    assert recovered.evidence[0].source_id == recovered.sources[0].id
    assert repository.delete_session(snapshot.state.id) is True
    assert repository.delete_session(snapshot.state.id) is False
    with pytest.raises(PersistenceNotFoundError):
        repository.load_session(snapshot.state.id)


def test_schema_version_mismatch_fails_predictably(repository):
    repository.initialize()
    connection = sqlite3.connect(repository.database_path)
    try:
        connection.execute(
            "UPDATE schema_metadata SET version = 99 WHERE component = 'research_sessions'"
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(PersistenceSchemaError, match="Unsupported SQLite"):
        repository.initialize()


def test_snapshot_rejects_broken_provenance():
    snapshot = make_snapshot()
    with pytest.raises(ValueError, match="missing source"):
        ResearchSessionSnapshot(
            request=snapshot.request,
            state=snapshot.state,
            plans=snapshot.plans,
            sources=[],
            evidence=snapshot.evidence,
        )


def test_historical_evidence_cannot_be_mutated_or_removed(repository):
    snapshot = make_snapshot()
    repository.save_session(snapshot)

    changed = snapshot.model_copy(deep=True)
    changed.evidence[0].content = "Silently rewritten evidence"
    with pytest.raises(PersistenceConflictError, match="historical evidence"):
        repository.save_session(changed)

    removed = snapshot.model_copy(deep=True)
    removed.claims = []
    with pytest.raises(PersistenceConflictError, match="historical claim"):
        repository.save_session(removed)


def test_secret_like_metadata_is_rejected_before_persistence(repository):
    snapshot = make_snapshot()
    snapshot.request.metadata["api_key"] = "must-not-be-written"

    with pytest.raises(SecretDataError, match="secret-like field"):
        repository.save_session(snapshot)
    assert not repository.list_sessions()


def test_memory_selects_relevant_evidence_and_preserves_provenance(repository):
    snapshot = make_snapshot()
    memory = BoundedResearchMemory(repository)
    memory.store(snapshot)

    results = memory.query(
        MemoryQuery(
            objective="solar battery storage",
            current_task="reliability evidence",
            limit=5,
        )
    )

    assert [result.evidence.id for result in results] == [snapshot.evidence[0].id]
    assert results[0].source.id == snapshot.sources[0].id
    assert results[0].claims[0].id == snapshot.claims[0].id
    assert results[0].task_ids == [snapshot.plans[0].tasks[0].id]
    assert results[0].session_id == snapshot.state.id


def test_memory_is_bounded_deduplicated_and_reconstructs(repository):
    snapshot = make_snapshot()
    memory = BoundedResearchMemory(repository)
    memory.store(snapshot)
    memory.store(snapshot)

    unfiltered = memory.query(MemoryQuery(limit=1))
    recovered = memory.retrieve(snapshot.state.id)

    assert len(unfiltered) == 1
    assert len(repository.list_sessions()) == 1
    assert recovered == snapshot
    assert recovered is not snapshot


def test_memory_ordering_and_limits_are_deterministic(repository):
    snapshot = make_snapshot()
    snapshot.evidence[1].content = snapshot.evidence[0].content
    memory = BoundedResearchMemory(repository)
    memory.store(snapshot)

    first = memory.query(MemoryQuery(objective="solar battery storage", limit=2))
    second = memory.query(MemoryQuery(objective="solar battery storage", limit=2))
    limited = memory.query(MemoryQuery(objective="solar battery storage", limit=1))

    expected_ids = [snapshot.evidence[0].id, snapshot.evidence[1].id]
    assert [record.evidence.id for record in first] == expected_ids
    assert first == second
    assert [record.evidence.id for record in limited] == expected_ids[:1]


def test_context_selection_is_deterministic_relevant_and_traceable(repository):
    snapshot = make_snapshot()
    memory = BoundedResearchMemory(repository)
    memory.store(snapshot)
    builder = ResearchContextBuilder(
        memory,
        ContextPolicy(
            max_characters=500,
            max_evidence=2,
            max_reflections=1,
            max_task_ids=1,
        ),
    )

    first = builder.build(
        snapshot.request,
        snapshot.state,
        current_task="Verify solar storage reliability",
    )
    second = builder.build(
        snapshot.request,
        snapshot.state,
        current_task="Verify solar storage reliability",
    )

    assert first == second
    assert [item.evidence_id for item in first.evidence] == [snapshot.evidence[0].id]
    assert snapshot.evidence[1].id not in {item.evidence_id for item in first.evidence}
    assert first.evidence[0].source_id == snapshot.sources[0].id
    assert first.evidence[0].claim_ids == [snapshot.claims[0].id]
    assert str(snapshot.evidence[0].id) in first.selection_trace[0]
    assert first.reflections == [snapshot.state.evaluation_history[-1]]
    assert first.completed_task_ids == [snapshot.plans[0].tasks[0].id]


def test_context_character_and_item_limits_are_respected(repository):
    snapshot = make_snapshot()
    snapshot.evidence[0].content = "solar storage " * 100
    memory = BoundedResearchMemory(repository)
    memory.store(snapshot)
    builder = ResearchContextBuilder(
        memory,
        ContextPolicy(
            max_characters=100,
            max_evidence=1,
            max_reflections=0,
            max_gaps=0,
        ),
    )

    context = builder.build(snapshot.request, snapshot.state, "solar reliability")

    assert context.text_character_count <= 100
    assert len(context.evidence) <= 1
    assert context.evidence[0].truncated is True


def make_orchestrator(
    repository: SQLiteResearchRepository | object | None = None,
    context_builder: ResearchContextBuilder | None = None,
) -> tuple[MasterResearchOrchestrator, ResearchRequest]:
    planning = MagicMock(spec=PlanningAgent)
    planning.create_plan = AsyncMock()
    research = MagicMock(spec=ResearchAgent)
    research.execute_task = AsyncMock()
    analysis = MagicMock(spec=AnalysisAgent)
    analysis.analyze = AsyncMock()
    evaluation = MagicMock(spec=EvaluationAgent)
    evaluation.evaluate = AsyncMock()
    report = MagicMock(spec=ReportAgent)
    report.generate_report = AsyncMock()
    request = ResearchRequest(id=UUID(int=2001), objective="Test persisted workflow")
    task = ResearchTask(
        id=UUID(int=2003),
        plan_id=UUID(int=2002),
        description="Execute workflow task",
        objective="workflow evidence",
    )
    plan = ResearchPlan(
        id=task.plan_id,
        request_id=request.id,
        objective=request.objective,
        tasks=[task],
    )
    planning.create_plan.return_value = plan
    research.execute_task.return_value = ToolResult(
        success=True,
        output={"summary": "done"},
        tool_name="web_search",
    )
    analysis.analyze.return_value = {"summary": "analyzed"}
    evaluation.evaluate.return_value = EvaluationResult(
        decision="COMPLETE",
        confidence=0.9,
    )
    report.generate_report.return_value = {"content": "report"}
    orchestrator = MasterResearchOrchestrator(
        planning_agent=planning,
        research_agent=research,
        analysis_agent=analysis,
        evaluation_agent=evaluation,
        report_agent=report,
        max_iterations=2,
        session_repository=repository,  # type: ignore[arg-type]
        context_builder=context_builder,
    )
    return orchestrator, request


@pytest.mark.asyncio
async def test_orchestrator_checkpoints_and_recovers_completed_session(repository):
    orchestrator, request = make_orchestrator(repository)

    completed = await orchestrator.start_research(request)
    persisted = repository.load_session(completed.id)
    recovering_orchestrator, _ = make_orchestrator(repository)
    recovered = recovering_orchestrator.recover_session(completed.id)

    assert completed.status == "completed"
    assert persisted.state == completed
    assert persisted.plans[0].tasks[0].status == "completed"
    assert persisted.report_metadata == {"content": "report"}
    assert recovered == completed
    assert recovered.status_history[-2:] == ["reporting", "completed"]


class FailingRepository:
    def initialize(self) -> None:
        raise AssertionError("not called directly")

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        raise PersistenceError("disk unavailable")

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        raise PersistenceError("disk unavailable")

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        raise PersistenceError("disk unavailable")

    def delete_session(self, session_id: UUID) -> bool:
        raise PersistenceError("disk unavailable")


@pytest.mark.asyncio
async def test_persistence_failure_terminates_without_entering_research_loop():
    orchestrator, request = make_orchestrator(FailingRepository())

    result = await orchestrator.start_research(request)

    assert result.status == "failed"
    assert result.status_history == ["initialized", "failed"]
    assert result.error == "Persistence failure: disk unavailable"
    orchestrator.planning_agent.create_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_supplies_bounded_context_when_configured(repository):
    memory = BoundedResearchMemory(repository)
    context_builder = ResearchContextBuilder(
        memory,
        ContextPolicy(max_characters=250, max_evidence=2),
    )
    orchestrator, request = make_orchestrator(repository, context_builder)

    result = await orchestrator.start_research(request)

    assert result.status == "completed"
    assert orchestrator.working_context is not None
    assert orchestrator.working_context.text_character_count <= 250
    assert orchestrator.planning_agent.create_plan.call_args.kwargs["context"] is not None
    assert orchestrator.research_agent.execute_task.call_args.kwargs["context"] is not None
    assert orchestrator.analysis_agent.analyze.call_args.kwargs["context"] is not None
    assert orchestrator.evaluation_agent.evaluate.call_args.kwargs["context"] is not None
    assert orchestrator.report_agent.generate_report.call_args.kwargs["context"] is not None
