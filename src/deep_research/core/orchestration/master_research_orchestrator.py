"""Master Research Orchestrator for coordinating the research workflow."""

from collections.abc import Mapping
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel

from deep_research.context.context_builder import AgentContext, ResearchContextBuilder
from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.planning_agent import PlanningAgent
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.agents.report_agent import ReportAgent
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import EvaluationRecord, ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.persistence.contracts import (
    PersistenceError,
    PersistenceNotFoundError,
    ResearchSessionRepository,
    ResearchSessionSnapshot,
)
from deep_research.runtime.contracts import RuntimeControlError
from deep_research.runtime.harness import ExecutionHarness
from deep_research.tools.tool import ToolResult


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
        session_repository: ResearchSessionRepository | None = None,
        context_builder: ResearchContextBuilder | None = None,
        execution_harness: ExecutionHarness | None = None,
    ):
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")

        self.planning_agent = planning_agent
        self.research_agent = research_agent
        self.analysis_agent = analysis_agent
        self.evaluation_agent = evaluation_agent
        self.report_agent = report_agent
        self.max_iterations = max_iterations
        self.session_repository = session_repository
        self.context_builder = context_builder
        self.execution_harness = execution_harness
        # Initialize with a dummy state; will be replaced in _initialize
        self.research_state: ResearchState = ResearchState(
            request_id=UUID(int=0), status="dummy", status_history=["dummy"]
        )
        self._request: Optional[ResearchRequest] = None
        self._plan: Optional[ResearchPlan] = None
        self._plans: List[ResearchPlan] = []
        self._analysis_result: Optional[Dict[str, Any]] = None
        self._working_context: AgentContext | None = None

    async def start_research(self, request: ResearchRequest) -> ResearchState:
        """
        Start a research run for the given request.
        Returns the final research state.
        """
        self._initialize(request)
        if self.execution_harness is not None:
            self.execution_harness.start_session()
        try:
            self._persist_checkpoint()
        except PersistenceError as exc:
            self._terminate_for_persistence_failure(exc)
            return self.research_state

        await self._run_research_loop()
        return self.research_state

    async def _run_research_loop(self) -> None:
        for iteration in range(self.max_iterations):
            self.research_state.iteration_number = iteration
            try:
                if self.execution_harness is not None:
                    self.execution_harness.begin_iteration()
                await self._execute_iteration()
                self._attach_runtime_report()
                self._persist_checkpoint()
            except RuntimeControlError as exc:
                self._terminate_for_runtime_failure(exc)
                self._attach_runtime_report()
                self._checkpoint_after_runtime_failure()
                break
            except PersistenceError as exc:
                self._terminate_for_persistence_failure(exc)
                break
            if self._is_terminal_state():
                break
        if not self._is_terminal_state():
            self._handle_max_iterations_exceeded()
            self._attach_runtime_report()
            try:
                self._persist_checkpoint()
            except PersistenceError as exc:
                self._terminate_for_persistence_failure(exc)

    def _checkpoint_after_runtime_failure(self) -> None:
        try:
            self._persist_checkpoint()
        except PersistenceError as persistence_error:
            self._terminate_for_persistence_failure(persistence_error)

    def recover_session(self, session_id: UUID) -> ResearchState:
        """Reconstruct orchestration state without executing another iteration."""
        if self.session_repository is None:
            raise PersistenceError(
                "Session recovery requires a research session repository",
                error_code="persistence_not_configured",
            )
        snapshot = self.session_repository.load_session(session_id)
        self._request = snapshot.request.model_copy(deep=True)
        self.research_state = snapshot.state.model_copy(deep=True)
        self._plans = [plan.model_copy(deep=True) for plan in snapshot.plans]
        self._plan = next(
            (
                plan
                for plan in self._plans
                if plan.id == self.research_state.current_plan_id
            ),
            None,
        )
        self._analysis_result = None
        self._working_context = None
        return self.research_state

    @property
    def working_context(self) -> AgentContext | None:
        """Most recently constructed bounded agent context, if configured."""
        return self._working_context

    def _initialize(self, request: ResearchRequest) -> None:
        self._request = request
        self.research_state = ResearchState(
            request_id=request.id,
            status="initialized",
            status_history=["initialized"],
        )
        self._plan = None
        self._plans = []
        self._analysis_result = None
        self._working_context = None

    async def _execute_iteration(self) -> None:
        await self._planning_phase()
        await self._research_phase()
        await self._analysis_phase()
        evaluation_result = await self._evaluation_phase()
        await self._handle_evaluation_result(evaluation_result)

    async def _planning_phase(self) -> None:
        assert self._request is not None, "Request must be set before planning"
        self._transition_to("planning")
        context = self._build_context()
        self._plan = await self._invoke_planning_agent(context)
        self._plans.append(self._plan)
        self.research_state.current_plan_id = self._plan.id
        self.research_state.plan_history.append(self._plan.id)
        self._transition_to("researching")

    async def _research_phase(self) -> None:
        assert self._plan is not None, "No plan available for research phase"
        completed_tasks = await self._execute_research_tasks(self._plan.tasks)
        # Update state with completed/failed tasks and gathered evidence/sources
        for task in completed_tasks:
            if task.status == "completed":
                self._extend_unique(self.research_state.completed_task_ids, [task.id])
                if task.evidence_gathered:
                    self._extend_unique(
                        self.research_state.gathered_evidence,
                        task.evidence_gathered,
                    )
                if task.sources_consulted:
                    self._extend_unique(
                        self.research_state.consulted_sources,
                        task.sources_consulted,
                    )
            elif task.status == "failed":
                self._extend_unique(self.research_state.failed_task_ids, [task.id])
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
            context = self._build_context(task.objective)
            tool_result = await self._invoke_research_agent(task, context)
            if tool_result.success:
                task.status = "completed"
                output = self._normalize_tool_output(tool_result.output)
                summary = output.get("summary")
                task.result = summary if isinstance(summary, str) else ""

                evidence_ids = output.get("evidence_ids")
                if isinstance(evidence_ids, list):
                    self._extend_unique(task.evidence_gathered, evidence_ids)

                source_ids = output.get("source_ids")
                if isinstance(source_ids, list):
                    self._extend_unique(task.sources_consulted, source_ids)
            else:
                task.status = "failed"
                task.error = tool_result.error or "Unknown error"
            completed_tasks.append(task)
        return completed_tasks

    async def _analysis_phase(self) -> None:
        assert self._plan is not None, "No plan available for analysis phase"
        self._transition_to("analyzing")
        context = self._build_context()
        self._analysis_result = await self._invoke_analysis_agent(
            self._plan, context
        )

    async def _evaluation_phase(self) -> EvaluationResult:
        assert self._plan is not None, "No plan available for evaluation phase"
        assert self._analysis_result is not None, (
            "No analysis result available for evaluation phase"
        )
        self._transition_to("evaluating")
        context = self._build_context()
        return await self._invoke_evaluation_agent(
            self._plan, self._analysis_result, context
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
        context = self._build_context()
        report = await self._invoke_report_agent(self._analysis_result, context)
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

    def _build_context(self, current_task: str | None = None) -> AgentContext | None:
        if self.context_builder is None:
            return None
        assert self._request is not None, "Request must be set before context creation"
        self._working_context = self.context_builder.build(
            request=self._request,
            state=self.research_state,
            current_task=current_task,
        )
        return self._working_context

    async def _invoke_planning_agent(
        self, context: AgentContext | None
    ) -> ResearchPlan:
        assert self._request is not None
        request = self._request

        async def operation() -> ResearchPlan:
            if context is None:
                return await self.planning_agent.create_plan(
                    request=request, state=self.research_state
                )
            return await self.planning_agent.create_plan(
                request=request,
                state=self.research_state,
                context=context,
            )

        if self.execution_harness is None:
            return await operation()
        return await self.execution_harness.execute_model("planning", operation)

    async def _invoke_research_agent(
        self, task: ResearchTask, context: AgentContext | None
    ) -> ToolResult:
        async def operation() -> ToolResult:
            if context is None:
                return await self.research_agent.execute_task(task)
            return await self.research_agent.execute_task(task, context=context)

        if self.execution_harness is None:
            return await operation()
        return await self.execution_harness.execute_tool(
            f"research_task:{task.id}",
            operation,
            external_api=bool(task.metadata.get("external_api", False)),
        )

    async def _invoke_analysis_agent(
        self, plan: ResearchPlan, context: AgentContext | None
    ) -> Dict[str, Any]:
        async def operation() -> Dict[str, Any]:
            if context is None:
                return await self.analysis_agent.analyze(self.research_state, plan)
            return await self.analysis_agent.analyze(
                self.research_state, plan, context=context
            )

        if self.execution_harness is None:
            return await operation()
        return await self.execution_harness.execute_model("analysis", operation)

    async def _invoke_evaluation_agent(
        self,
        plan: ResearchPlan,
        analysis_result: Dict[str, Any],
        context: AgentContext | None,
    ) -> EvaluationResult:
        async def operation() -> EvaluationResult:
            if context is None:
                return await self.evaluation_agent.evaluate(
                    self.research_state, plan, analysis_result
                )
            return await self.evaluation_agent.evaluate(
                self.research_state, plan, analysis_result, context=context
            )

        if self.execution_harness is None:
            return await operation()
        return await self.execution_harness.execute_model("evaluation", operation)

    async def _invoke_report_agent(
        self, analysis_result: Dict[str, Any], context: AgentContext | None
    ) -> Dict[str, Any]:
        async def operation() -> Dict[str, Any]:
            if context is None:
                return await self.report_agent.generate_report(
                    self.research_state, analysis_result
                )
            return await self.report_agent.generate_report(
                self.research_state, analysis_result, context=context
            )

        if self.execution_harness is None:
            return await operation()
        return await self.execution_harness.execute_model("reporting", operation)

    def _persist_checkpoint(self) -> None:
        if self.session_repository is None:
            return
        assert self._request is not None, "Request must be set before persistence"

        existing: ResearchSessionSnapshot | None = None
        try:
            existing = self.session_repository.load_session(self.research_state.id)
        except PersistenceNotFoundError:
            pass

        plans_by_id = {
            plan.id: plan.model_copy(deep=True)
            for plan in (existing.plans if existing else [])
        }
        for plan in self._plans:
            plans_by_id[plan.id] = plan.model_copy(deep=True)

        report = self.research_state.metadata.get("report")
        report_metadata = (
            dict(report)
            if isinstance(report, Mapping)
            else dict(existing.report_metadata) if existing else {}
        )
        snapshot = ResearchSessionSnapshot(
            request=self._request.model_copy(deep=True),
            state=self.research_state.model_copy(deep=True),
            plans=list(plans_by_id.values()),
            sources=list(existing.sources) if existing else [],
            evidence=list(existing.evidence) if existing else [],
            claims=list(existing.claims) if existing else [],
            evidence_task_links=dict(existing.evidence_task_links) if existing else {},
            report_metadata=report_metadata,
        )
        self.session_repository.save_session(snapshot)

    def _terminate_for_persistence_failure(self, error: PersistenceError) -> None:
        self.research_state.status = "failed"
        if not self.research_state.status_history or self.research_state.status_history[-1] != "failed":
            self.research_state.status_history.append("failed")
        self.research_state.error = f"Persistence failure: {error.message}"

    def _terminate_for_runtime_failure(self, error: RuntimeControlError) -> None:
        self.research_state.status = "failed"
        if (
            not self.research_state.status_history
            or self.research_state.status_history[-1] != "failed"
        ):
            self.research_state.status_history.append("failed")
        self.research_state.error = f"Runtime failure: {error.message}"
        self.research_state.metadata["runtime_failure"] = {
            "error_code": error.error_code,
            "failure_kind": error.failure_kind.value,
            "operation": error.operation,
            "operation_kind": (
                error.operation_kind.value if error.operation_kind else None
            ),
            "attempts": error.attempts,
            "details": error.details,
        }

    def _attach_runtime_report(self) -> None:
        if self.execution_harness is not None:
            self.research_state.metadata["runtime"] = (
                self.execution_harness.report().model_dump(mode="json")
            )

    @staticmethod
    def _extend_unique(target: List[UUID], values: List[Any]) -> None:
        known = set(target)
        for value in values:
            if isinstance(value, UUID) and value not in known:
                target.append(value)
                known.add(value)

    @staticmethod
    def _normalize_tool_output(output: Any) -> Mapping[str, Any]:
        if isinstance(output, BaseModel):
            return output.model_dump()
        if isinstance(output, Mapping):
            return output
        return {}
