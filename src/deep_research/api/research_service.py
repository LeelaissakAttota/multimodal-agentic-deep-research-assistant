"""Application service connecting the bounded orchestrator to product APIs."""

from __future__ import annotations

import math
from collections.abc import Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from deep_research.core.agents.analysis_agent import DeterministicAnalysisAgent
from deep_research.core.agents.deterministic_research_agent import (
    DeterministicResearchAgent,
)
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.agents.evaluation_agent import DeterministicEvaluationAgent, EvaluationAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent, PlanningAgent
from deep_research.core.agents.report_agent import DeterministicReportAgent
from deep_research.verification.fact_check import ClaimVerifier
from deep_research.core.config import Settings
from deep_research.core.orchestration.master_research_orchestrator import (
    MasterResearchOrchestrator,
)
from deep_research.core.orchestration.step_gate import StepGate
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchAttachment, ResearchRequest
from deep_research.context.context_builder import ResearchContextBuilder
from deep_research.memory.factory import LongTermMemory, build_long_term_memory
from deep_research.models.model_gateway import ModelGateway
from deep_research.runtime.contracts import OperationKind
from deep_research.runtime.harness import ExecutionHarness, InMemoryRuntimeObserver
from deep_research.security import find_sensitive_data_path


class ResearchSubmission(BaseModel):
    """Validated product request for one bounded research run."""

    model_config = ConfigDict(extra="forbid")

    objective: str = Field(min_length=1, max_length=10_000)
    metadata: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=50,
    )
    attachments: list[ResearchAttachment] = Field(default_factory=list, max_length=5)

    @field_validator("objective")
    @classmethod
    def validate_objective(cls, value: str) -> str:
        objective = value.strip()
        if not objective:
            raise ValueError("objective cannot be blank")
        if "\x00" in objective:
            raise ValueError("objective cannot contain null characters")
        return objective

    @field_validator("metadata")
    @classmethod
    def validate_metadata(
        cls, value: dict[str, str | int | float | bool | None]
    ) -> dict[str, str | int | float | bool | None]:
        sensitive_path = find_sensitive_data_path(value, "metadata")
        if sensitive_path is not None:
            raise ValueError(f"secret-like metadata is not accepted at {sensitive_path}")
        for key, item in value.items():
            if not key.strip() or len(key) > 100:
                raise ValueError("metadata keys must contain 1 to 100 characters")
            if isinstance(item, str) and len(item) > 2_000:
                raise ValueError("metadata string values cannot exceed 2000 characters")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("metadata numbers must be finite")
        return dict(value)


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
        self._run_gateway: ModelGateway | None = None
        self._run_budget_hook: Callable[[str], None] | None = None
        self._memory: LongTermMemory | None = None
        self._memory_built = False

    async def submit(
        self, submission: ResearchSubmission, *, step_gate: StepGate | None = None
    ) -> ResearchRunResponse:
        orchestrator = self._build_orchestrator()
        orchestrator.step_gate = step_gate
        request = ResearchRequest(
            objective=submission.objective.strip(),
            metadata=dict(submission.metadata),
            attachments=list(submission.attachments),
        )
        state = await orchestrator.start_research(request)
        report = state.metadata.get("report")
        if isinstance(report, dict):
            report["agent_mode"] = "llm_with_fallback" if self.agentic else "deterministic"
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
        if self.agentic:
            # LLM tool choice runs inside one research task: leave room for its decision window
            # plus search, page reads and synthesis so the harness does not cut it off.
            floor = self.settings.agent_decision_budget_seconds + 45.0
            if limits.max_tool_call_time_seconds < floor:
                limits = limits.model_copy(update={"max_tool_call_time_seconds": floor})
        harness = ExecutionHarness(
            limits,
            observer=InMemoryRuntimeObserver(max_events=1_000),
        )
        gateway = self._agent_gateway()
        sources = self.settings.enabled_source_tools() if self.settings.research_mode == "live" else []
        planning_agent: PlanningAgent = DeterministicPlanningAgent(sources)
        evaluation_agent: EvaluationAgent = DeterministicEvaluationAgent()
        if gateway is not None:
            from deep_research.core.agents.llm_agents import LlmEvaluationAgent, LlmPlanningAgent

            planning_agent = LlmPlanningAgent(
                gateway, max_tasks=self.settings.agent_max_plan_tasks,
                max_iterations=limits.max_research_iterations,
                timeout_seconds=self.settings.agent_llm_timeout_seconds,
                source_tools=sources,
            )
            evaluation_agent = LlmEvaluationAgent(
                gateway, max_iterations=limits.max_research_iterations,
                timeout_seconds=self.settings.agent_llm_timeout_seconds,
            )

        def charge_model_call(operation: str) -> None:
            harness.budget.before_call(OperationKind.MODEL, operation=operation)

        self._run_gateway, self._run_budget_hook = gateway, charge_model_call
        memory = self.long_term_memory

        return MasterResearchOrchestrator(
            planning_agent=planning_agent,
            research_agent=self._build_research_agent(),
            analysis_agent=DeterministicAnalysisAgent(),
            evaluation_agent=evaluation_agent,
            report_agent=DeterministicReportAgent(
                verifier=ClaimVerifier(gateway, timeout_seconds=self.settings.agent_llm_timeout_seconds,
                                       budget_hook=charge_model_call if gateway is not None else None)
            ),
            max_iterations=limits.max_research_iterations,
            execution_harness=harness,
            session_repository=memory.repository if memory else None,
            context_builder=ResearchContextBuilder(memory.memory) if memory else None,
        )

    @property
    def long_term_memory(self) -> LongTermMemory | None:
        """Lazily built, cached long-term memory (``None`` when ``long_term_memory=off``)."""
        if not self._memory_built:
            self._memory = build_long_term_memory(self.settings.long_term_memory, self.settings.memory_dir)
            self._memory_built = True
        return self._memory

    def build_research_agent(self) -> ResearchAgent:
        """Public accessor used by the control API to re-run a single step."""
        return self._build_research_agent()

    @property
    def agentic(self) -> bool:
        """True when LLM agents decide planning, evaluation and tool choice for this run."""
        if self.settings.research_mode != "live" or self.settings.agent_mode == "deterministic":
            return False
        key = self.settings.openrouter_api_key
        return bool(key and key.get_secret_value().strip())

    def _agent_gateway(self) -> ModelGateway | None:
        if not self.agentic:
            return None
        from deep_research.core.free_model_guard import ModelCapability
        from deep_research.core.provider_wiring import build_provider_gateway

        return build_provider_gateway(self.settings, capability=ModelCapability.TEXT_GENERATION)

    def _build_research_agent(self) -> ResearchAgent:
        """Pick the research agent from ``settings.research_mode``.

        ``offline`` (default) keeps the deterministic, network-free agent.
        ``live`` uses real web search/fetch plus optional LLM synthesis via the
        free-model-guarded OpenRouter gateway (snippet summary when no key).
        """
        if self.settings.research_mode != "live":
            return DeterministicResearchAgent()
        from deep_research.core.agents.live_research_agent import LiveResearchAgent
        from deep_research.core.free_model_guard import ModelCapability
        from deep_research.core.provider_wiring import build_provider_gateway
        from deep_research.tools.document_tool import DocumentReaderTool
        from deep_research.tools.image_tool import ImageAnalysisTool
        from deep_research.tools.pdf_tool import PdfReaderTool
        from deep_research.tools.web_page_tool import WebPageReaderTool
        from deep_research.tools.registry import build_source_tools
        from deep_research.tools.web_tools import WebFetchTool, WebSearchTool

        vision = build_provider_gateway(self.settings, capability=ModelCapability.VISION)

        return LiveResearchAgent(
            search_tool=WebSearchTool(
                provider=self.settings.search_provider,
                tavily_api_key=self.settings.tavily_api_key,
            ),
            fetch_tool=WebFetchTool(),
            gateway=build_provider_gateway(self.settings, capability=ModelCapability.TEXT_GENERATION),
            max_results=self.settings.live_max_search_results,
            max_fetch=self.settings.live_max_fetch_pages,
            max_tokens=self.settings.max_tokens_per_call,
            media_tools={
                "pdf_reader": PdfReaderTool(vision_gateway=vision),
                "image_analyzer": ImageAnalysisTool(gateway=vision, use_settings=False),
                "document_parser": DocumentReaderTool(),
                "web_page_reader": WebPageReaderTool(WebFetchTool(max_chars=60_000)),
            },
            tool_gateway=self._run_gateway if self.settings.agent_tool_steps > 0 else None,
            tool_steps=self.settings.agent_tool_steps,
            max_searches=2,
            decision_budget_seconds=self.settings.agent_decision_budget_seconds,
            budget_hook=self._run_budget_hook,
            source_tools=build_source_tools(self.settings.enabled_source_tools()),
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
