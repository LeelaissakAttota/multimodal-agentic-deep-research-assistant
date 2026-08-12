"""
Unit tests for the Master Research Orchestrator and agents.
"""
from unittest.mock import AsyncMock, MagicMock
import pytest
from uuid import UUID

from deep_research.core.orchestration.master_research_orchestrator import MasterResearchOrchestrator
from deep_research.domain.research_request import ResearchRequest
from deep_research.core.agents.planning_agent import PlanningAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.report_agent import ReportAgent


@pytest.fixture
def mock_request():
    return ResearchRequest(
        id=UUID(int=1),
        objective="Test research objective",
        context="Test context",
    )


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
def orchestrator(mock_planning_agent, mock_research_agent, mock_analysis_agent, mock_evaluation_agent, mock_report_agent):
    return MasterResearchOrchestrator(
        planning_agent=mock_planning_agent,
        research_agent=mock_research_agent,
        analysis_agent=mock_analysis_agent,
        evaluation_agent=mock_evaluation_agent,
        report_agent=mock_report_agent,
        max_iterations=2,
    )


@pytest.mark.asyncio
class TestMasterResearchOrchestrator:
    async def test_start_research_initializes_state(self, orchestrator, mock_request):
        # Arrange
        # We don't need to set up the agents' return values for this test, but we should avoid errors.
        orchestrator._planning_phase = AsyncMock()
        orchestrator._research_phase = AsyncMock()
        orchestrator._analysis_phase = AsyncMock()
        orchestrator._evaluation_phase = AsyncMock(return_value=EvaluationResult(decision="COMPLETE"))
        orchestrator._reporting_phase = AsyncMock()

        # Act
        await orchestrator.start_research(mock_request)

        # Assert
        assert orchestrator.research_state is not None
        assert orchestrator.research_state.request_id == mock_request.id
        assert orchestrator.research_state.status == "reporting"  # Because we mocked evaluation to return COMPLETE

    async def test_start_respects_max_iterations(self, orchestrator, mock_request):
        # Arrange
        # Make the evaluation agent return CONTINUE every time so we hit max iterations
        orchestrator._planning_phase = AsyncMock()
        orchestrator._research_phase = AsyncMock()
        orchestrator._analysis_phase = AsyncMock()
        orchestrator._evaluation_phase = AsyncMock(return_value=EvaluationResult(decision="CONTINUE"))
        orchestrator._reporting_phase = AsyncMock()

        # Act
        final_state = await orchestrator.start_research(mock_request)

        # Assert
        # We set max_iterations=2, so we should have run 2 iterations
        assert final_state.iteration_number == 1  # 0-indexed, so after 2 iterations, iteration_number is 1
        assert final_state.status == "failed"  # Because we exceeded max iterations without completion

    async def test_start_research_handles_complete_evaluation(self, orchestrator, mock_request):
        # Arrange
        orchestrator._planning_phase = AsyncMock()
        orchestrator._research_phase = AsyncMock()
        orchestrator._analysis_phase = AsyncMock()
        orchestrator._evaluation_phase = AsyncMock(return_value=EvaluationResult(decision="COMPLETE"))
        orchestrator._reporting_phase = AsyncMock()

        # Act
        final_state = await orchestrator.start_research(mock_request)

        # Assert
        assert final_state.status == "reporting"
        orchestrator._reporting_phase.assert_awaited_once()

    async def test_start_research_handles_blocked_evaluation(self, orchestrator, mock_request):
        # Arrange
        orchestrator._planning_phase = AsyncMock()
        orchestrator._research_phase = AsyncMock()
        orchestrator._analysis_phase = AsyncMock()
        orchestrator._evaluation_phase = AsyncMock(return_value=EvaluationResult(decision="BLOCKED"))
        orchestrator._reporting_phase = AsyncMock()

        # Act
        final_state = await orchestrator.start_research(mock_request)

        # Assert
        assert final_state.status == "blocked"
        orchestrator._reporting_phase.assert_not_awaited()

    async def test_start_research_handles_failed_evaluation(self, orchestrator, mock_request):
        # Arrange
        orchestrator._planning_phase = AsyncMock()
        orchestrator._research_phase = AsyncMock()
        orchestrator._analysis_phase = AsyncMock()
        orchestrator._evaluation_phase = AsyncMock(return_value=EvaluationResult(decision="FAILED"))
        orchestrator._reporting_phase = AsyncMock()

        # Act
        final_state = await orchestrator.start_research(mock_request)

        # Assert
        assert final_state.status == "failed"
        orchestrator._reporting_phase.assert_not_awaited()

    # We can also test the internal phases with more detailed mocks, but for now, this is sufficient to test the orchestrator's flow.
