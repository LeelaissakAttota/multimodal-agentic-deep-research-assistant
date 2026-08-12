"""
Master Research Orchestrator for coordinating the research workflow.
"""
from typing import Any, Dict, List, Optional
from uuid import UUID

from deep_research.domain.research_request import ResearchRequest
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.core.agents.planning_agent import PlanningAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.report_agent import ReportAgent
from deep_research.tools.tool import ToolRequest


class MasterResearchOrchestrator:
    """
    Orchestrates the research workflow by delegating to specialized agents.
    """

    def __init__(
        self,
        planning_agent: PlanningAgent,
        research_agent: ResearchAgent,
        analysis_agent: AnalysisAgent,
        evaluation_agent: EvaluationAgent,
        report_agent: ReportAgent,
        max_iterations: int = 3,
    ):
        self.planning_agent = planning_agent
        self.research_agent = research_agent
        self.analysis_agent = analysis_agent
        self.evaluation_agent = evaluation_agent
        self.report_agent = report_agent
        self.max_iterations = max_iterations
        # Initialize with a dummy state; will be replaced in _initialize
        self.research_state: ResearchState = ResearchState(
            request_id=UUID(int=0), status="dummy"
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
            request_id=request.id, status="initialized"
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
        self.research_state.status = "planning"
        self._plan = await self.planning_agent.create_plan(
            request=self._request,
            state=self.research_state,
        )
        self.research_state.current_plan_id = self._plan.id
        self.research_state.plan_history.append(self._plan.id)
        self.research_state.status = "researching"

    async def _research_phase(self) -> None:
        assert self._plan is not None, "No plan available for research phase"
        self.research_state.status = "researching"
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
            # Create a tool request from the task
            tool_request = ToolRequest(
                tool_name=task.assigned_tool or "",
                parameters=task.tool_input or {},
            )
            # Execute the task using the research agent
            tool_result = await self.research_agent.execute_task(task, tool_request)
            # Update the task with the result
            if tool_result.success:
                task.status = "completed"
                task.result = tool_result.output.get("summary", "")
                # Extract evidence and source IDs from the tool result if available
                if "evidence_ids" in tool_result.output:
                    task.evidence_gathered.extend(tool_result.output["evidence_ids"])
                if "source_ids" in tool_result.output:
                    task.sources_consulted.extend(tool_result.output["source_ids"])
            else:
                task.status = "failed"
                task.error = tool_result.error or "Unknown error"
            completed_tasks.append(task)
        return completed_tasks

    async def _analysis_phase(self) -> None:
        assert self._plan is not None, "No plan available for analysis phase"
        self.research_state.status = "analyzing"
        self._analysis_result = await self.analysis_agent.analyze(
            self.research_state,
            self._plan,
        )

    async def _evaluation_phase(self) -> EvaluationResult:
        assert self._plan is not None, "No plan available for evaluation phase"
        assert self._analysis_result is not None, "No analysis result available for evaluation phase"
        self.research_state.status = "evaluating"
        evaluation_result = await self.evaluation_agent.evaluate(
            self.research_state,
            self._plan,
            self._analysis_result,
        )
        self.research_state.evaluation_result = evaluation_result.decision
        return evaluation_result

    async def _handle_evaluation_result(self, evaluation_result: EvaluationResult) -> None:
        if evaluation_result.decision == "COMPLETE":
            self.research_state.status = "reporting"
            await self._reporting_phase()
        elif evaluation_result.decision == "BLOCKED":
            self.research_state.status = "blocked"
        elif evaluation_result.decision == "FAILED":
            self.research_state.status = "failed"
        # For CONTINUE, we do nothing; the loop will continue and the state will be updated in the next iteration.

    async def _reporting_phase(self) -> None:
        assert self._analysis_result is not None, "No analysis result available for reporting phase"
        report = await self.report_agent.generate_report(
            self.research_state,
            self._analysis_result,
        )
        self.research_state.metadata["report"] = report

    def _is_terminal_state(self) -> bool:
        return self.research_state.status in ("completed", "failed", "blocked", "reporting")

    def _handle_max_iterations_exceeded(self) -> None:
        if not self._is_terminal_state():
            # If we exited the loop due to max iterations and haven't reached a terminal state, mark as failed.
            self.research_state.status = "failed"
            self.research_state.error = (
                f"Research failed to complete within {self.max_iterations} iterations"
            )
