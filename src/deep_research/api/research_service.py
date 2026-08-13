"""Application service connecting the bounded orchestrator to product APIs."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from deep_research.core.agents.analysis_agent import DeterministicAnalysisAgent
from deep_research.core.agents.deterministic_research_agent import (
    DeterministicResearchAgent,
)
from deep_research.core.agents.evaluation_agent import DeterministicEvaluationAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent
from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.core.config import Settings
from deep_research.core.orchestration.master_research_orchestrator import (
    MasterResearchOrchestrator,
)
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchRequest
from deep_research.runtime.harness import ExecutionHarness, InMemoryRuntimeObserver


class ResearchSubmission(BaseModel):
    """Validated product request for one bounded research run."""

    objective: str = Field(min_length=1, max_length=10_000)
    metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict
    )

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        objective = value.strip()
        if not objective:
            raise ValueError("objective cannot be blank")
        return objective


class ResearchRunResponse(BaseModel):
    """Traceable API representation of a terminal research run."""

    session_id: UUID
    request_id: UUID
    status: str
    error: str | None = None
    report: dict[str, object] | None = None
    runtime: dict[str, object] | None = None


class ResearchApplication:
    """Small in-process application boundary for submitting and reading runs."""

    def __init__(self, settings: Settings, max_sessions: int = 100) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1")
        self.settings = settings
        self.max_sessions = max_sessions
        self._states: dict[UUID, ResearchState] = {}

    async def submit(self, submission: ResearchSubmission) -> ResearchRunResponse:
        orchestrator = self._build_orchestrator()
        request = ResearchRequest(
            objective=submission.objective.strip(),
            metadata=dict(submission.metadata),
        )
        state = await orchestrator.start_research(request)
        self._states[state.id] = state.model_copy(deep=True)
        while len(self._states) > self.max_sessions:
            oldest_session_id = next(iter(self._states))
            del self._states[oldest_session_id]
        return self._response(state)

    def get(self, session_id: UUID) -> ResearchRunResponse | None:
        state = self._states.get(session_id)
        return self._response(state) if state is not None else None

    def _build_orchestrator(self) -> MasterResearchOrchestrator:
        limits = self.settings.runtime_limits()
        harness = ExecutionHarness(
            limits,
            observer=InMemoryRuntimeObserver(max_events=1_000),
        )
        return MasterResearchOrchestrator(
            planning_agent=DeterministicPlanningAgent(),
            research_agent=DeterministicResearchAgent(),
            analysis_agent=DeterministicAnalysisAgent(),
            evaluation_agent=DeterministicEvaluationAgent(),
            report_agent=DeterministicReportAgent(),
            max_iterations=limits.max_research_iterations,
            execution_harness=harness,
        )

    @staticmethod
    def _response(state: ResearchState) -> ResearchRunResponse:
        report = state.metadata.get("report")
        runtime = state.metadata.get("runtime")
        return ResearchRunResponse(
            session_id=state.id,
            request_id=state.request_id,
            status=state.status,
            error=state.error,
            report=dict(report) if isinstance(report, dict) else None,
            runtime=dict(runtime) if isinstance(runtime, dict) else None,
        )
