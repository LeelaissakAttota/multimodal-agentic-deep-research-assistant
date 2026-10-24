"""Master Research Orchestrator for coordinating the research workflow."""

from collections.abc import Mapping
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel

from deep_research.context.context_builder import AgentContext, ResearchContextBuilder
from deep_research.core.attachment_context import ATTACHMENT_TOOL_KINDS, attachment_entry
from deep_research.core.agents.analysis_agent import AnalysisAgent
from deep_research.core.agents.evaluation_agent import EvaluationAgent, EvaluationResult
from deep_research.core.agents.llm_support import DECISIONS_KEY, record_decision
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
from deep_research.runtime.contracts import FailureKind, RuntimeControlError
from deep_research.core.orchestration.step_gate import GateOutcome, StepGate
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
        step_gate: StepGate | None = None,
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
        # Optional human-in-the-loop gate; None keeps the original behaviour.
        self.step_gate = step_gate
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
            metadata={"objective": request.objective},
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
        outcome = await self._gate_phase("planning")
        try:
            self._plan = await self._invoke_planning_agent(context)
        except Exception as exc:
            await self._gate_after(outcome, ok=False, error=str(exc))
            raise
        await self._gate_after(outcome, ok=True)
        self._trace([{"event": "plan", "task": str(t.description)[:300], "tool": str(t.assigned_tool or "")}
                     for t in self._plan.tasks])
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
            outcome = await self._gate_task(task)
            if outcome is not None and not outcome.approved:
                task.status = "failed"
                task.error = "rejected by operator"
                completed_tasks.append(task)
                continue
            self._inject_attachment_context(task)
            try:
                tool_result = await self._invoke_research_agent(task, context)
            except Exception as exc:
                await self._gate_after(outcome, ok=False, error=str(exc))
                raise
            finally:
                task.metadata.pop("attachment_context", None)
            await self._gate_after(
                outcome,
                ok=tool_result.success,
                output=self._normalize_tool_output(tool_result.output).get("summary") if tool_result.success else None,
                error=None if tool_result.success else (tool_result.error or "Unknown error"),
            )
            self._trace(self._source_events(tool_result))
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
                self._record_attachment(task, output)
                self._record_live_finding(task, output, tool_result.metadata)
            else:
                task.status = "failed"
                task.error = tool_result.error or "Unknown error"
            self._record_tool_decision(task, tool_result.metadata)
            completed_tasks.append(task)
        return completed_tasks

    def _record_tool_decision(self, task: ResearchTask, metadata: Mapping[str, Any]) -> None:
        """Log who chose the task's tool calls (only for agents that report a tool policy)."""
        policy = metadata.get("tool_policy") if isinstance(metadata, Mapping) else None
        if policy not in ("llm", "deterministic", "llm_then_deterministic"):
            return
        record_decision(self.research_state, {
            "agent": "researcher", "source": policy, "task": task.metadata.get("section") or task.objective,
            "model_id": metadata.get("tool_model_id"), "fallback_reason": metadata.get("tool_fallback_reason"),
            "status": task.status, "trace": metadata.get("tool_trace") or [],
        })

    def _inject_attachment_context(self, task: ResearchTask) -> None:
        """Hand processed attachments to non-attachment tasks as primary sources (transient)."""
        entries = self.research_state.metadata.get("attachment_context")
        if isinstance(entries, list) and entries and task.assigned_tool not in ATTACHMENT_TOOL_KINDS:
            task.metadata["attachment_context"] = list(entries)

    def _record_attachment(self, task: ResearchTask, output: Mapping[str, Any]) -> None:
        entry = attachment_entry(task.assigned_tool, output)
        if entry is None:
            return
        entries = self.research_state.metadata.setdefault("attachment_context", [])
        if isinstance(entries, list):
            entries.append(entry)

    def _record_live_finding(
        self, task: ResearchTask, output: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> None:
        """Keep live-mode synthesis and real source metadata for the report.

        Only populated when the research agent returns ``source_objects``
        (live mode); offline/deterministic runs are unaffected.
        """
        source_objects = output.get("source_objects")
        if not isinstance(source_objects, list) or not source_objects:
            return
        sources = [row for row in (_source_row(source) for source in source_objects) if row is not None]
        if not sources:
            return
        excerpts = _evidence_excerpts(output.get("evidence_objects"))
        for row in sources:
            if row["source_id"] in excerpts:
                row["excerpt"] = excerpts[row["source_id"]]
        findings = self.research_state.metadata.setdefault("live_findings", [])
        if isinstance(findings, list):
            findings.append(
                {
                    "task_id": str(task.id),
                    "objective": task.objective,
                    "section": task.metadata.get("section"),
                    "kind": "attachment" if task.assigned_tool in ATTACHMENT_TOOL_KINDS else "web",
                    "query": metadata.get("query"),
                    "summary": output.get("summary") if isinstance(output.get("summary"), str) else "",
                    "attachment": {k: output.get(k) for k in ("page_count", "has_text", "note", "image_count")}
                    if task.assigned_tool in ATTACHMENT_TOOL_KINDS else None,
                    "model_id": metadata.get("model_id"),
                    "synthesis": metadata.get("synthesis"),
                    "synthesis_error": metadata.get("synthesis_error"),
                    "sources": sources,
                }
            )

    async def _analysis_phase(self) -> None:
        assert self._plan is not None, "No plan available for analysis phase"
        self._transition_to("analyzing")
        context = self._build_context()
        outcome = await self._gate_phase("analysis")
        try:
            self._analysis_result = await self._invoke_analysis_agent(
                self._plan, context
            )
        except Exception as exc:
            await self._gate_after(outcome, ok=False, error=str(exc))
            raise
        await self._gate_after(outcome, ok=True)

    async def _evaluation_phase(self) -> EvaluationResult:
        assert self._plan is not None, "No plan available for evaluation phase"
        assert self._analysis_result is not None, (
            "No analysis result available for evaluation phase"
        )
        self._transition_to("evaluating")
        context = self._build_context()
        outcome = await self._gate_phase("evaluation")
        try:
            evaluation = await self._invoke_evaluation_agent(self._plan, self._analysis_result, context)
        except Exception as exc:
            await self._gate_after(outcome, ok=False, error=str(exc))
            raise
        await self._gate_after(outcome, ok=True, output={"decision": evaluation.decision})
        self._trace([{"event": "verdict", "decision": str(evaluation.decision),
                      "gaps": [str(g)[:200] for g in (evaluation.gaps or [])[:5]]}])
        return evaluation

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
        outcome = await self._gate_phase("reporting")
        try:
            report = await self._invoke_report_agent(self._analysis_result, context)
        except Exception as exc:
            await self._gate_after(outcome, ok=False, error=str(exc))
            raise
        await self._gate_after(outcome, ok=True)
        decisions = self.research_state.metadata.get(DECISIONS_KEY)
        if isinstance(decisions, list) and decisions and isinstance(report, dict):
            report["agent_decisions"] = list(decisions)
            report["agent_summary"] = _decision_summary(decisions)
            if isinstance(report.get("content"), str) and report.get("citations") is not None:
                report["content"] = report["content"] + "\n\n" + _decision_markdown(decisions)
        self.research_state.metadata["report"] = report
        if self.research_state.error:  # a task failed earlier but the run recovered
            self.research_state.metadata["last_task_error"] = self.research_state.error
            self.research_state.error = None
        self._transition_to("completed")

    async def _gate_phase(self, kind: str) -> GateOutcome | None:
        if self.step_gate is None:
            return None
        outcome = await self.step_gate.before(
            kind,
            f"{kind} (iteration {self.research_state.iteration_number})",
            {"iteration": self.research_state.iteration_number},
        )
        if not outcome.approved:
            reason = "stopped by operator" if outcome.stopped else "rejected by operator"
            raise RuntimeControlError(
                f"{kind} {reason}",
                failure_kind=FailureKind.EMERGENCY_STOP,
                operation=kind,
            )
        return outcome

    async def _gate_task(self, task: ResearchTask) -> GateOutcome | None:
        if self.step_gate is None:
            return None
        outcome = await self.step_gate.before(
            "research_task",
            task.description[:120],
            {"objective": task.objective, "description": task.description, "tool": task.assigned_tool},
        )
        if outcome.stopped:
            raise RuntimeControlError(
                "research stopped by operator",
                failure_kind=FailureKind.EMERGENCY_STOP,
                operation="research_task",
            )
        if outcome.approved:
            objective = outcome.inputs.get("objective")
            if isinstance(objective, str) and objective.strip():
                task.objective = objective.strip()
            description = outcome.inputs.get("description")
            if isinstance(description, str) and description.strip():
                task.description = description.strip()
        return outcome

    async def _gate_after(
        self, outcome: GateOutcome | None, *, ok: bool, output: Any = None, error: str | None = None
    ) -> None:
        if self.step_gate is not None and outcome is not None:
            await self.step_gate.after(outcome, ok=ok, output=output, error=error)

    def _trace(self, events: List[Dict[str, Any]]) -> None:
        """Hand real progress events (plan tasks, queries, sources, verdicts) to an optional gate ``trace``."""

        trace = getattr(self.step_gate, "trace", None)
        if callable(trace) and events:
            try:
                trace(events)
            except Exception:  # noqa: BLE001 - tracing must never break research
                pass

    @staticmethod
    def _source_events(tool_result: Any) -> List[Dict[str, Any]]:
        meta = tool_result.metadata if isinstance(getattr(tool_result, "metadata", None), dict) else {}
        out = tool_result.output if isinstance(getattr(tool_result, "output", None), dict) else {}
        queries = meta.get("queries") or ([meta["query"]] if meta.get("query") else [])
        events: List[Dict[str, Any]] = [{"event": "search", "query": str(q)[:300]} for q in queries if q]
        seen: set[str] = set()

        def _val(obj: Any, key: str) -> str:
            raw = obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)
            return "" if raw is None else str(raw)

        for item in [*(out.get("source_objects") or []), *(out.get("results") or [])][:20]:
            url, title = _val(item, "url"), _val(item, "title")
            key = url or title
            if not key or key in seen:
                continue
            seen.add(key)
            snippet = _val(item, "snippet") or _val(item, "description") or ("" if url else "attached file")
            events.append({"event": "source", "url": url[:500], "title": title[:200], "snippet": snippet[:240]})
        return events

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


def _source_row(source: Any) -> Optional[Dict[str, Any]]:
    """JSON-safe citation row: web sources need a URL; attachment sources are labelled instead."""
    source_id = getattr(source, "id", None)
    if source_id is None:
        return None
    url = getattr(source, "url", None)
    metadata = getattr(source, "metadata", None) or {}
    is_attachment = bool(metadata.get("attachment")) if isinstance(metadata, dict) else False
    if url is None and not is_attachment:
        return None
    title = getattr(source, "title", None) or (str(url) if url else "Attachment")
    row: Dict[str, Any] = {"source_id": str(source_id), "url": str(url) if url else None, "title": title}
    if is_attachment and isinstance(metadata, dict):
        row["kind"] = "attachment"
        row["page"] = metadata.get("page")
    return row


_EXCERPT_CHARS = 2000


def _evidence_excerpts(evidence_objects: Any) -> Dict[str, str]:
    """Source id -> concatenated evidence text (capped), so claims can be checked against what was read."""
    out: Dict[str, str] = {}
    if not isinstance(evidence_objects, list):
        return out
    for item in evidence_objects:
        source_id, content = getattr(item, "source_id", None), getattr(item, "content", None)
        if source_id is None or not isinstance(content, str) or not content.strip():
            continue
        key = str(source_id)
        joined = (out.get(key, "") + "\n" + content.strip()).strip()
        out[key] = joined[:_EXCERPT_CHARS]
    return out


def _decision_summary(decisions: List[Any]) -> Dict[str, Dict[str, int]]:
    """Count decisions per agent and source, e.g. {"planner": {"llm": 2}}."""
    summary: Dict[str, Dict[str, int]] = {}
    for entry in decisions:
        if isinstance(entry, dict):
            per_agent = summary.setdefault(str(entry.get("agent")), {})
            source = str(entry.get("source"))
            per_agent[source] = per_agent.get(source, 0) + 1
    return summary


def _decision_markdown(decisions: List[Any]) -> str:
    """Short, human-readable log of agent decisions for the report."""
    lines = ["## How this research was run", ""]
    for entry in decisions:
        if not isinstance(entry, dict):
            continue
        label = f"Round {int(entry.get('iteration', 0)) + 1} - {entry.get('agent')} ({entry.get('source')})"
        if entry.get("fallback_reason"):
            label += f", fallback: {entry['fallback_reason']}"
        agent = entry.get("agent")
        if agent == "planner":
            queries = [t.get("query") for t in entry.get("tasks") or [] if isinstance(t, dict) and t.get("query")]
            detail = "; ".join(str(q) for q in queries)
        elif agent == "evaluator":
            detail = f"{entry.get('decision')}" + (f" - gaps: {'; '.join(entry.get('gaps') or [])}"
                                                    if entry.get("gaps") else "")
        else:
            steps = [t for t in entry.get("trace") or [] if isinstance(t, dict)]
            detail = " -> ".join(str(t.get("action")) for t in steps)
        lines.append(f"- {label}: {detail}" if detail else f"- {label}")
    return "\n".join(lines)
