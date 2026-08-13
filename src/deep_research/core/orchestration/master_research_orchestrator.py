"""Master Research Orchestrator for coordinating the research workflow."""

from collections.abc import Mapping
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel

from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.planning_agent import PlanningAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.agents.report_agent import ReportAgent
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import EvaluationRecord, ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest


class MasterResearchOrchestrator:
    """
    Orchestrates a bounded research state graph using specialized agents.
    """

    _STATE_TRANSITIONS: Dict[str, frozenset[str]] = {
        "initialized": frozenset({"planning"}),
        "planning": frozenset({"researching"}),
        "researching": frozenset({"analyzing"}),
        "analyzing": frozenset({"evaluating"}),
        "evaluating": frozenset({"planning", "reporting", "blocked", "failed"}),
        "reporting": frozenset({"completed", "failed"}),
        "completed": frozenset(),
        "blocked": frozenset(),
        "failed": frozenset(),
    }

    def __init__(
        self,
        planning_agent: PlanningAgent,
        research_agent: ResearchAgent,
        analysis_agent: AnalysisAgent,
        evaluation_agent: EvaluationAgent,
        report_agent: ReportAgent,
        max_iterations: int = 3,
    ):
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")

        self.planning_agent = planning_agent
        self.research_agent = research_agent
        self.analysis_agent = analysis_agent
        self.evaluation_agent = evaluation_agent
        self.report_agent = report_agent
        self.max_iterations = max_iterations
        # Initialize with a dummy state; will be replaced in _initialize
        self.research_state: ResearchState = ResearchState(
            request_id=UUID(int=0), status="dummy", status_history=["dummy"]
        )
        self._request: Optional[ResearchRequest] = None
        self._plan: Optional[ResearchPlan] = None
        self._analysis_result: Optional[Dict[str, Any]] = None

    async def start_research(self, request: ResearchRequest) -> ResearchState:
        """
        Start a research run for the given request.
        Returns the final research state.
        """
        self._initialize(request)
        for iteration in range(self.max_iterations):
            self.research_state.iteration_number = iteration
            await self._execute_iteration()
            if self._is_terminal_state():
                break
        self._handle_max_iterations_exceeded()
        return self.research_state

    def _initialize(self, request: ResearchRequest) -> None:
        self._request = request
        self.research_state = ResearchState(
            request_id=request.id,
            status="initialized",
            status_history=["initialized"],
        )
        self._plan = None
        self._analysis_result = None

    async def _execute_iteration(self) -> None:
        await self._planning_phase()
        await self._research_phase()
        await self._analysis_phase()
        evaluation_result = await self._evaluation_phase()
        await self._handle_evaluation_result(evaluation_result)

    async def _planning_phase(self) -> None:
        assert self._request is not None, "Request must be set before planning"
        self._transition_to("planning")
        self._plan = await self.planning_agent.create_plan(
            request=self._request,
            state=self.research_state,
        )
        self.research_state.current_plan_id = self._plan.id
        self.research_state.plan_history.append(self._plan.id)
        self._transition_to("researching")

    async def _research_phase(self) -> None:
        assert self._plan is not None, "No plan available for research phase"
        completed_tasks = await self._execute_research_tasks(self._plan.tasks)
        # Update state with completed/failed tasks and gathered evidence/sources
        for task in completed_tasks:
            if task.status == "completed":
                self.research_state.completed_task_ids.append(task.id)
                if task.evidence_gathered:
                    self.research_state.gathered_evidence.extend(task.evidence_gathered)
                if task.sources_consulted:
                    self.research_state.consulted_sources.extend(task.sources_consulted)
            elif task.status == "failed":
                self.research_state.failed_task_ids.append(task.id)
                if task.error:
                    self.research_state.error = task.error
        # Note: We assume that the research phase does not change the plan itself.

    async def _execute_research_tasks(self, tasks: List[ResearchTask]) -> List[ResearchTask]:
        """
        Execute a list of research tasks using the research agent.
        Returns the list of tasks with updated status and results.
        """
        completed_tasks: List[ResearchTask] = []
        for task in tasks:
            tool_result = await self.research_agent.execute_task(task)
            if tool_result.success:
                task.status = "completed"
                output = self._normalize_tool_output(tool_result.output)
                summary = output.get("summary")
                task.result = summary if isinstance(summary, str) else ""

                evidence_ids = output.get("evidence_ids")
                if isinstance(evidence_ids, list):
                    task.evidence_gathered.extend(evidence_ids)

                source_ids = output.get("source_ids")
                if isinstance(source_ids, list):
                    task.sources_consulted.extend(source_ids)
            else:
                task.status = "failed"
                task.error = tool_result.error or "Unknown error"
            completed_tasks.append(task)
        return completed_tasks

    async def _analysis_phase(self) -> None:
        assert self._plan is not None, "No plan available for analysis phase"
        self._transition_to("analyzing")
        self._analysis_result = await self.analysis_agent.analyze(
            self.research_state,
            self._plan,
        )

    async def _evaluation_phase(self) -> EvaluationResult:
        assert self._plan is not None, "No plan available for evaluation phase"
        assert self._analysis_result is not None, (
            "No analysis result available for evaluation phase"
        )
        self._transition_to("evaluating")
        return await self.evaluation_agent.evaluate(
            self.research_state,
            self._plan,
            self._analysis_result,
        )

    async def _handle_evaluation_result(self, evaluation_result: EvaluationResult) -> None:
        self.research_state.last_evaluation_gaps = list(evaluation_result.gaps)
        self.research_state.last_evaluation_reasoning = evaluation_result.reasoning
        self.research_state.last_evaluation_confidence = evaluation_result.confidence
        self.research_state.evaluation_result = evaluation_result.decision
        self.research_state.evaluation_history.append(
            EvaluationRecord(
                iteration_number=self.research_state.iteration_number,
                decision=evaluation_result.decision,
                confidence=evaluation_result.confidence,
                reasoning=evaluation_result.reasoning,
                gaps=evaluation_result.gaps,
            )
        )

        if evaluation_result.decision == "COMPLETE":
            self._transition_to("reporting")
            await self._reporting_phase()
        elif evaluation_result.decision == "BLOCKED":
            self._transition_to("blocked")
        elif evaluation_result.decision == "FAILED":
            self._transition_to("failed")

    async def _reporting_phase(self) -> None:
        assert self._analysis_result is not None, (
            "No analysis result available for reporting phase"
        )
        report = await self.report_agent.generate_report(
            self.research_state,
            self._analysis_result,
        )
        self.research_state.metadata["report"] = report
        self._transition_to("completed")

    def _is_terminal_state(self) -> bool:
        return self.research_state.status in ("completed", "failed", "blocked")

    def _handle_max_iterations_exceeded(self) -> None:
        if not self._is_terminal_state():
            self._transition_to("failed")
            self.research_state.error = (
                f"Research failed to complete within {self.max_iterations} iterations"
            )

    def _transition_to(self, next_status: str) -> None:
        current_status = self.research_state.status
        allowed_statuses = self._STATE_TRANSITIONS.get(current_status, frozenset())
        if next_status not in allowed_statuses:
            raise RuntimeError(
                f"Invalid research state transition: {current_status} -> {next_status}"
            )
        self.research_state.status = next_status
        self.research_state.status_history.append(next_status)

    @staticmethod
    def _normalize_tool_output(output: Any) -> Mapping[str, Any]:
        if isinstance(output, BaseModel):
            return output.model_dump()
        if isinstance(output, Mapping):
            return output
        return {}
