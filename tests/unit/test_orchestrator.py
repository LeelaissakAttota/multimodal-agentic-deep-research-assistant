"""Tests for the bounded deep-research workflow and its agent contracts."""

from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import (
    DeterministicEvaluationAgent,
    EvaluationAgent,
    EvaluationResult,
)
from deep_research.core.agents.planning_agent import (
    DeterministicPlanningAgent,
    PlanningAgent,
)
from deep_research.core.agents.report_agent import ReportAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.orchestration.master_research_orchestrator import (
    MasterResearchOrchestrator,
)
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.tools.tool import ToolResult


class StructuredToolOutput(BaseModel):
    summary: str
    evidence_ids: list[UUID]
    source_ids: list[UUID]


@pytest.fixture
def mock_planning_agent():
    agent = MagicMock(spec=PlanningAgent)
    agent.create_plan = AsyncMock()
    return agent


@pytest.fixture
def mock_research_agent():
    agent = MagicMock(spec=ResearchAgent)
    agent.execute_task = AsyncMock()
    return agent


@pytest.fixture
def mock_analysis_agent():
    agent = MagicMock(spec=AnalysisAgent)
    agent.analyze = AsyncMock()
    return agent


@pytest.fixture
def mock_evaluation_agent():
    agent = MagicMock(spec=EvaluationAgent)
    agent.evaluate = AsyncMock()
    return agent


@pytest.fixture
def mock_report_agent():
    agent = MagicMock(spec=ReportAgent)
    agent.generate_report = AsyncMock()
    return agent


@pytest.fixture
def orchestrator(
    mock_planning_agent,
    mock_research_agent,
    mock_analysis_agent,
    mock_evaluation_agent,
    mock_report_agent,
):
    return MasterResearchOrchestrator(
        planning_agent=mock_planning_agent,
        research_agent=mock_research_agent,
        analysis_agent=mock_analysis_agent,
        evaluation_agent=mock_evaluation_agent,
        report_agent=mock_report_agent,
        max_iterations=2,
    )


@pytest.fixture
def mock_request():
    return ResearchRequest(
        id=UUID(int=1),
        objective="Test research objective",
        context="Test context",
    )


def make_plan(request: ResearchRequest, objective: str = "Dummy objective") -> ResearchPlan:
    plan = ResearchPlan(
        request_id=request.id,
        objective=request.objective,
        tasks=[
            ResearchTask(
                plan_id=UUID(int=0),
                description=objective,
                objective=objective,
                assigned_tool="web_search",
                tool_input={"query": objective},
            )
        ],
    )
    plan.tasks[0].plan_id = plan.id
    return plan


def configure_successful_phases(
    orchestrator: MasterResearchOrchestrator,
    request: ResearchRequest,
) -> None:
    orchestrator.planning_agent.create_plan.return_value = make_plan(request)
    orchestrator.research_agent.execute_task.return_value = ToolResult(
        success=True,
        output={"summary": "dummy"},
        tool_name="web_search",
    )
    orchestrator.analysis_agent.analyze.return_value = {"summary": "analysis"}
    orchestrator.report_agent.generate_report.return_value = {"content": "report"}


def test_orchestrator_rejects_unbounded_configuration(
    mock_planning_agent,
    mock_research_agent,
    mock_analysis_agent,
    mock_evaluation_agent,
    mock_report_agent,
):
    with pytest.raises(ValueError, match="max_iterations must be at least 1"):
        MasterResearchOrchestrator(
            planning_agent=mock_planning_agent,
            research_agent=mock_research_agent,
            analysis_agent=mock_analysis_agent,
            evaluation_agent=mock_evaluation_agent,
            report_agent=mock_report_agent,
            max_iterations=0,
        )


@pytest.mark.asyncio
async def test_complete_workflow_reaches_completed_state(orchestrator, mock_request):
    configure_successful_phases(orchestrator, mock_request)
    orchestrator.evaluation_agent.evaluate.return_value = EvaluationResult(
        decision="COMPLETE",
        confidence=0.9,
        reasoning="Sufficient evidence gathered",
    )

    result = await orchestrator.start_research(mock_request)

    assert result.request_id == mock_request.id
    assert result.status == "completed"
    assert result.status_history == [
        "initialized",
        "planning",
        "researching",
        "analyzing",
        "evaluating",
        "reporting",
        "completed",
    ]
    assert result.metadata["report"] == {"content": "report"}
    assert result.evaluation_result == "COMPLETE"
    assert result.last_evaluation_reasoning == "Sufficient evidence gathered"
    assert len(result.evaluation_history) == 1
    orchestrator.report_agent.generate_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_workflow_accepts_structured_phase_three_tool_output(
    orchestrator,
    mock_request,
):
    configure_successful_phases(orchestrator, mock_request)
    evidence_id = uuid4()
    source_id = uuid4()
    orchestrator.research_agent.execute_task.return_value = ToolResult(
        success=True,
        output=StructuredToolOutput(
            summary="structured result",
            evidence_ids=[evidence_id],
            source_ids=[source_id],
        ),
        tool_name="web_search",
    )
    orchestrator.evaluation_agent.evaluate.return_value = EvaluationResult(
        decision="COMPLETE",
        confidence=0.9,
    )

    result = await orchestrator.start_research(mock_request)

    assert result.gathered_evidence == [evidence_id]
    assert result.consulted_sources == [source_id]


@pytest.mark.asyncio
async def test_continue_reflects_and_replans_with_state_snapshot(
    orchestrator,
    mock_request,
):
    configure_successful_phases(orchestrator, mock_request)
    snapshots = []

    async def create_plan(request, state):
        snapshots.append(state.model_copy(deep=True))
        return make_plan(request, objective=f"Iteration {len(snapshots)}")

    orchestrator.planning_agent.create_plan.side_effect = create_plan
    orchestrator.evaluation_agent.evaluate.side_effect = [
        EvaluationResult(
            decision="CONTINUE",
            confidence=0.6,
            reasoning="Need more evidence",
            gaps=["Gap 1", "Gap 2"],
        ),
        EvaluationResult(
            decision="COMPLETE",
            confidence=0.9,
            reasoning="Gaps resolved",
        ),
    ]

    result = await orchestrator.start_research(mock_request)

    assert result.status == "completed"
    assert len(snapshots) == 2
    assert snapshots[0].last_evaluation_gaps == []
    assert snapshots[0].last_evaluation_reasoning is None
    assert snapshots[1].last_evaluation_gaps == ["Gap 1", "Gap 2"]
    assert snapshots[1].last_evaluation_reasoning == "Need more evidence"
    assert snapshots[1].last_evaluation_confidence == 0.6
    assert [record.decision for record in result.evaluation_history] == [
        "CONTINUE",
        "COMPLETE",
    ]
    assert result.evaluation_history[0].gaps == ["Gap 1", "Gap 2"]
    assert len(result.plan_history) == 2


@pytest.mark.asyncio
async def test_continue_stops_at_iteration_bound(orchestrator, mock_request):
    configure_successful_phases(orchestrator, mock_request)
    orchestrator.planning_agent.create_plan.side_effect = [
        make_plan(mock_request, "Iteration 1"),
        make_plan(mock_request, "Iteration 2"),
    ]
    orchestrator.evaluation_agent.evaluate.return_value = EvaluationResult(
        decision="CONTINUE",
        confidence=0.5,
        reasoning="More research required",
        gaps=["Unresolved evidence gap"],
    )

    result = await orchestrator.start_research(mock_request)

    assert result.status == "failed"
    assert result.iteration_number == 1
    assert result.error == "Research failed to complete within 2 iterations"
    assert len(result.evaluation_history) == 2
    assert orchestrator.evaluation_agent.evaluate.await_count == 2
    assert orchestrator.report_agent.generate_report.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("decision", "expected_status"),
    [("BLOCKED", "blocked"), ("FAILED", "failed")],
)
async def test_terminal_evaluation_decisions(
    orchestrator,
    mock_request,
    decision,
    expected_status,
):
    configure_successful_phases(orchestrator, mock_request)
    orchestrator.evaluation_agent.evaluate.return_value = EvaluationResult(
        decision=decision,
        confidence=0.8,
        reasoning=f"Research is {expected_status}",
    )

    result = await orchestrator.start_research(mock_request)

    assert result.status == expected_status
    assert result.evaluation_result == decision
    assert orchestrator.report_agent.generate_report.await_count == 0


@pytest.mark.asyncio
async def test_deterministic_planner_builds_executable_initial_plan(mock_request):
    planner = DeterministicPlanningAgent()
    state = ResearchState(request_id=mock_request.id)

    plan = await planner.create_plan(mock_request, state)

    assert len(plan.tasks) == 3
    assert all(task.plan_id == plan.id for task in plan.tasks)
    assert all(task.assigned_tool == "web_search" for task in plan.tasks)
    assert all(task.tool_input and task.tool_input["limit"] == 5 for task in plan.tasks)


@pytest.mark.asyncio
async def test_deterministic_planner_replans_from_unique_gaps(mock_request):
    planner = DeterministicPlanningAgent()
    state = ResearchState(
        request_id=mock_request.id,
        last_evaluation_gaps=["  Recent evidence ", "Expert opinion", "Recent evidence"],
    )

    plan = await planner.create_plan(mock_request, state)

    assert [task.description for task in plan.tasks] == [
        "Address gap: Recent evidence",
        "Address gap: Expert opinion",
    ]
    assert [task.tool_input["query"] for task in plan.tasks if task.tool_input] == [
        "Recent evidence",
        "Expert opinion",
    ]


@pytest.mark.asyncio
async def test_deterministic_evaluator_detects_and_resolves_evidence_gap(mock_request):
    evaluator = DeterministicEvaluationAgent()
    plan = make_plan(mock_request)
    state = ResearchState(
        request_id=mock_request.id,
        completed_task_ids=[plan.tasks[0].id],
    )

    incomplete = await evaluator.evaluate(state, plan, {})
    state.gathered_evidence.append(uuid4())
    complete = await evaluator.evaluate(state, plan, {})

    assert incomplete.decision == "CONTINUE"
    assert incomplete.gaps == ["Insufficient evidence gathered"]
    assert complete.decision == "COMPLETE"
    assert complete.gaps == []


def test_evaluation_result_validates_and_normalizes_feedback():
    result = EvaluationResult(
        decision="CONTINUE",
        confidence=0.5,
        gaps=[" Evidence gap ", "Evidence gap", ""],
    )

    assert result.gaps == ["Evidence gap"]
    with pytest.raises(ValueError, match="Unsupported evaluation decision"):
        EvaluationResult(decision="RETRY")
    with pytest.raises(ValueError, match="confidence"):
        EvaluationResult(decision="CONTINUE", confidence=1.1)
