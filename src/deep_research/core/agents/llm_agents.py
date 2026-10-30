"""LLM planning and evaluation agents with per-call deterministic fallback.

``LlmPlanningAgent`` decides which web research tasks to run (and, on replan
iterations, how to close the evaluator's gaps). ``LlmEvaluationAgent`` judges
coverage, citation sufficiency and quality and returns COMPLETE / CONTINUE /
FAILED plus concrete gaps, which drive the orchestrator's existing replan loop.

Both validate the model's JSON strictly. On rate limit, provider error,
timeout, invalid JSON or a schema violation, that single decision is made by
the wrapped deterministic agent instead, and the decision log says so.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from uuid import UUID

from deep_research.context.context_builder import AgentContext
from deep_research.core.agents.evaluation_agent import (
    DeterministicEvaluationAgent,
    EvaluationAgent,
    EvaluationResult,
)
from deep_research.core.agents.llm_support import (
    AgentDecisionError,
    ask_json,
    clean_text,
    record_decision,
)
from deep_research.core.agents.planning_agent import (
    SOURCE_ROUTES,
    DeterministicPlanningAgent,
    PlanningAgent,
    _attachment_tasks,
)
from deep_research.tools.registry import SOURCE_TOOLS
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.models.model_gateway import ModelGateway

_PLAN_PROMPT = """You are the planning agent of a web research system.
Decide which web research tasks to run next. Each task is one web search followed by reading the best pages.
Return ONLY a JSON object, no prose:
{{"rationale": "<one sentence>", "tasks": [
  {{"section": "<short report heading>", "query": "<search query>", "focus": "<what to extract>"{tool_field}}}]}}
Rules: 1 to {max_tasks} tasks; queries must be specific and different from already-run queries; no duplicate queries.
{tools}
Research question: {question}
Iteration: {iteration} of {max_iterations}
{attachments}{history}{gaps}"""

# Planner prompt variants for A/B experiments; "default" adds nothing.
PLAN_STYLES = {
    "default": "",
    "broad": "Style: cover distinct angles of the question (background, evidence, counterpoints) with one task each.\n",
    "focused": "Style: prefer fewer, very precise queries that target primary sources and hard numbers.\n",
}

_EVAL_PROMPT = """You are the evaluation (reflection) agent of a web research system.
Judge whether the research gathered so far answers the question well enough to write a cited report.
Score coverage (are all parts of the question answered), citation_sufficiency (are claims backed by several
independent sources) and quality (are findings specific, current, consistent), each 0-5.
Decide: "COMPLETE" (good enough to report), "CONTINUE" (another research round would materially improve it;
list 1-3 concrete gaps phrased as things to find out), or "FAILED" (nothing usable can be found).
Return ONLY a JSON object, no prose:
{{"decision": "COMPLETE|CONTINUE|FAILED", "confidence": 0.0-1.0, "reasoning": "<one or two sentences>",
"scores": {{"coverage": 0-5, "citation_sufficiency": 0-5, "quality": 0-5}}, "gaps": ["..."]}}

Research question: {question}
Iteration: {iteration} of {max_iterations} ({remaining} research rounds left after this one)
Distinct sources gathered so far: {sources}
Findings so far:
{findings}"""


class LlmPlanningAgent(PlanningAgent):
    def __init__(
        self,
        gateway: ModelGateway,
        *,
        fallback: PlanningAgent | None = None,
        max_tasks: int = 4,
        max_iterations: int = 3,
        timeout_seconds: float = 40.0,
        max_tokens: int = 1_500,
        source_tools: List[str] | None = None,
        preferred: List[str] | None = None,
        saved_topics: List[str] | None = None,
        style: str = "default",
        preamble: str = "",
    ) -> None:
        if max_tasks < 1:
            raise ValueError("max_tasks must be at least 1")
        self.gateway = gateway
        self.source_tools = [t for t in (source_tools or []) if t in SOURCE_ROUTES]
        self.fallback = fallback or DeterministicPlanningAgent(self.source_tools, preferred or (), saved_topics or ())
        self.max_tasks = max_tasks
        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self.style = style if style in PLAN_STYLES else "default"
        self.preamble = preamble  # agent identity + retrieved lessons (deep_research.learning)

    async def create_plan(
        self,
        request: ResearchRequest,
        state: ResearchState,
        context: AgentContext | None = None,
    ) -> ResearchPlan:
        gaps = [gap for gap in state.last_evaluation_gaps if gap.strip()]
        try:
            reply, model_id = await ask_json(
                self.gateway,
                self._prompt(request, state, gaps),
                max_tokens=self.max_tokens,
                timeout_seconds=self.timeout_seconds,
            )
            web_tasks, rationale = self._validate(reply, request, state)
        except AgentDecisionError as exc:
            plan = await self.fallback.create_plan(request, state, context)
            record_decision(state, {
                "agent": "planner", "source": "deterministic", "fallback_reason": exc.reason,
                "detail": exc.detail[:200], "tasks": [_task_brief(t) for t in plan.tasks],
            })
            return plan
        tasks = [*(_attachment_tasks(request) if not gaps else []), *web_tasks]
        plan = ResearchPlan(request_id=request.id, objective=request.objective, tasks=tasks)
        for task in plan.tasks:
            task.plan_id = plan.id
            task.metadata["planned_by"] = "llm"
        record_decision(state, {
            "agent": "planner", "source": "llm", "model_id": model_id, "rationale": rationale,
            "replan": bool(gaps), "tasks": [_task_brief(t) for t in plan.tasks],
        })
        return plan

    def _prompt(self, request: ResearchRequest, state: ResearchState, gaps: List[str]) -> str:
        attachments = ""
        if request.attachments:
            names = ", ".join(f"{a.kind} '{a.name or i}'" for i, a in enumerate(request.attachments, 1))
            attachments = f"User attachments (read automatically, do not plan tasks for them): {names}\n"
        queries = _previous_queries(state)
        history = "Already-run queries:\n" + "\n".join(f"- {q}" for q in queries) + "\n" if queries else ""
        gap_text = (
            "The evaluator found these gaps; plan tasks that close them:\n"
            + "\n".join(f"- {g}" for g in gaps) + "\n"
        ) if gaps else ""
        return self.preamble + _PLAN_PROMPT.format(
            max_tasks=self.max_tasks, question=request.objective,
            iteration=state.iteration_number + 1, max_iterations=self.max_iterations,
            attachments=attachments, history=history, gaps=gap_text,
            tool_field=', "tool": "web_search|' + "|".join(self.source_tools) + '"' if self.source_tools else "",
            tools=self._tools_text(),
        ) + PLAN_STYLES[self.style]

    def _tools_text(self) -> str:
        if not self.source_tools:
            return ""
        lines = ["Tools (pick the best one per task; default web_search):",
                 "- web_search: general web pages"]
        lines += [f"- {t}: {SOURCE_TOOLS[t][1]}" for t in self.source_tools]
        return "\n".join(lines) + "\n"

    def _validate(
        self, reply: Dict[str, Any], request: ResearchRequest, state: ResearchState
    ) -> tuple[List[ResearchTask], str]:
        raw = reply.get("tasks")
        if not isinstance(raw, list) or not raw:
            raise AgentDecisionError("invalid_plan", "tasks must be a non-empty list")
        if len(raw) > self.max_tasks:
            raise AgentDecisionError("invalid_plan", f"{len(raw)} tasks exceeds max {self.max_tasks}")
        seen = {q.lower() for q in _previous_queries(state)}
        tasks: List[ResearchTask] = []
        for item in raw:
            if not isinstance(item, dict):
                raise AgentDecisionError("invalid_plan", "task is not an object")
            query = clean_text(item.get("query"), max_len=300)
            if len(query) < 3:
                raise AgentDecisionError("invalid_plan", "task query missing or too short")
            if query.lower() in seen:
                continue
            seen.add(query.lower())
            section = clean_text(item.get("section"), max_len=80) or query[:60]
            focus = clean_text(item.get("focus"), max_len=300)
            tool = clean_text(item.get("tool"), max_len=40) or "web_search"
            if tool != "web_search" and tool not in self.source_tools:
                raise AgentDecisionError("invalid_plan", f"tool {tool!r} not allowed")
            metadata: Dict[str, Any] = {"section": section, "question": request.objective, "focus": focus}
            if tool != "web_search":
                metadata["source_tool"] = tool
            tasks.append(ResearchTask(
                plan_id=UUID(int=0),
                description=f"Research {section.lower()} for: {request.objective}",
                objective=f"{section}: {focus or query}",
                assigned_tool=tool,
                tool_input={"query": query, "limit": 2 if tool == "youtube_transcript" else 5},
                metadata=metadata,
            ))
        if not tasks:
            raise AgentDecisionError("invalid_plan", "every proposed query was a duplicate")
        return tasks, clean_text(reply.get("rationale"), max_len=400)


class LlmEvaluationAgent(EvaluationAgent):
    def __init__(
        self,
        gateway: ModelGateway,
        *,
        fallback: EvaluationAgent | None = None,
        max_iterations: int = 3,
        timeout_seconds: float = 40.0,
        max_tokens: int = 1_200,
    ) -> None:
        self.gateway = gateway
        self.fallback = fallback or DeterministicEvaluationAgent()
        self.max_iterations = max_iterations
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens

    async def evaluate(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        analysis_result: Dict[str, Any],
        context: AgentContext | None = None,
    ) -> EvaluationResult:
        try:
            reply, model_id = await ask_json(
                self.gateway, self._prompt(state, plan),
                max_tokens=self.max_tokens, timeout_seconds=self.timeout_seconds,
            )
            result, guard = self._validate(reply, state)
        except AgentDecisionError as exc:
            result = await self.fallback.evaluate(state, plan, analysis_result, context)
            record_decision(state, {
                "agent": "evaluator", "source": "deterministic", "fallback_reason": exc.reason,
                "detail": exc.detail[:200], "decision": result.decision, "gaps": result.gaps,
                "reasoning": result.reasoning,
            })
            return result
        record_decision(state, {
            "agent": "evaluator", "source": "llm", "model_id": model_id, "decision": result.decision,
            "confidence": result.confidence, "reasoning": result.reasoning, "gaps": result.gaps,
            "scores": result.metadata.get("scores"), **({"guard": guard} if guard else {}),
        })
        return result

    def _prompt(self, state: ResearchState, plan: ResearchPlan) -> str:
        findings = state.metadata.get("live_findings")
        lines: List[str] = []
        if isinstance(findings, list):
            for f in findings[-8:]:
                if isinstance(f, dict):
                    lines.append(
                        f"- [{f.get('section') or 'Findings'}] query={f.get('query')!r} "
                        f"sources={len(f.get('sources') or [])}: {str(f.get('summary') or '')[:700]}"
                    )
        for task in plan.tasks:
            if task.status == "failed":
                lines.append(f"- [{task.metadata.get('section') or 'task'}] FAILED: {(task.error or '')[:200]}")
            elif not isinstance(findings, list) and task.status == "completed":
                lines.append(f"- [{task.metadata.get('section') or 'task'}] {(task.result or '')[:700]}")
        iteration = state.iteration_number + 1
        return _EVAL_PROMPT.format(
            question=json.dumps(plan.objective)[1:-1], iteration=iteration,
            max_iterations=self.max_iterations, remaining=max(self.max_iterations - iteration, 0),
            sources=len(state.consulted_sources), findings="\n".join(lines) or "- (none)",
        )

    def _validate(self, reply: Dict[str, Any], state: ResearchState) -> tuple[EvaluationResult, str | None]:
        decision = str(reply.get("decision") or "").strip().upper()
        if decision not in {"COMPLETE", "CONTINUE", "FAILED", "BLOCKED"}:
            raise AgentDecisionError("invalid_evaluation", f"decision {decision!r} not allowed")
        try:
            confidence = min(max(float(reply.get("confidence", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError) as exc:
            raise AgentDecisionError("invalid_evaluation", "confidence is not a number") from exc
        raw_gaps = reply.get("gaps") or []
        if not isinstance(raw_gaps, list):
            raise AgentDecisionError("invalid_evaluation", "gaps must be a list")
        gaps = [g for g in (clean_text(x, max_len=200) for x in raw_gaps) if g][:3]
        scores = reply.get("scores") if isinstance(reply.get("scores"), dict) else {}
        has_evidence = bool(state.gathered_evidence)
        guard: str | None = None
        if decision == "COMPLETE" and not has_evidence:
            raise AgentDecisionError("invalid_evaluation", "COMPLETE without any gathered evidence")
        if decision == "CONTINUE" and not gaps:
            raise AgentDecisionError("invalid_evaluation", "CONTINUE requires at least one gap")
        if decision == "CONTINUE" and state.iteration_number + 1 >= self.max_iterations:
            # No research rounds left: report what we have rather than fail on the iteration cap.
            decision = "COMPLETE" if has_evidence else "FAILED"
            guard = "last_iteration_continue_converted"
        if decision == "COMPLETE":
            gaps = [] if guard is None else gaps
        return EvaluationResult(
            decision=decision, confidence=confidence,
            reasoning=clean_text(reply.get("reasoning"), max_len=600) or None,
            gaps=gaps if decision == "CONTINUE" else [],
            metadata={"scores": scores, "source": "llm", "open_gaps": gaps},
        ), guard


def _previous_queries(state: ResearchState) -> List[str]:
    findings = state.metadata.get("live_findings")
    queries: List[str] = []
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict) and isinstance(f.get("query"), str) and f["query"] not in queries:
                queries.append(f["query"])
    return queries


def _task_brief(task: ResearchTask) -> Dict[str, Any]:
    return {
        "section": task.metadata.get("section"),
        "tool": task.assigned_tool,
        "query": (task.tool_input or {}).get("query"),
    }
