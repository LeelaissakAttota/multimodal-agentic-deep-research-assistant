"""LLM planner / evaluator / tool-choice agents and their deterministic fallbacks (offline)."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.agents.llm_agents import LlmEvaluationAgent, LlmPlanningAgent
from deep_research.core.agents.llm_support import AgentDecisionError, parse_json_object
from deep_research.core.config import Settings
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research.research_task import ResearchTask
from deep_research.domain.research_request import ResearchRequest
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse
from deep_research.tools.web_tools import WebFetchTool, WebSearchTool

DDG_HTML = """
<html><body>
<div class="result"><a class="result__a" href="https://example.org/a">Alpha page</a>
<a class="result__snippet">Alpha snippet text</a></div>
<div class="result"><a class="result__a" href="https://example.net/b">Beta</a>
<a class="result__snippet">Beta snippet</a></div>
</body></html>
"""
PAGE = "<html><head><title>Alpha Title</title></head><body><p>Alpha body facts.</p></body></html>"

Reply = str | BaseException | Callable[[], Any]


def web_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "html.duckduckgo.com":
        return httpx.Response(200, text=DDG_HTML)
    if request.url.host in {"example.org", "example.net"}:
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    return httpx.Response(404)


class ScriptedLLM(ModelGateway):
    """Answers by role (planner / evaluator / tool choice / synthesis) from per-role queues."""

    def __init__(self, **queues: list[Reply]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: dict[str, int] = {}

    @staticmethod
    def role(prompt: str) -> str:
        if "planning agent" in prompt:
            return "plan"
        if "evaluation (reflection) agent" in prompt:
            return "eval"
        if "choosing which tool" in prompt:
            return "tool"
        return "synth"

    async def generate(self, request: ModelRequest) -> ModelResponse:
        role = self.role(request.prompt)
        self.calls[role] = self.calls.get(role, 0) + 1
        queue = self.queues.get(role) or []
        reply: Reply = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else "Alpha is a fact [1].")
        if callable(reply) and not isinstance(reply, BaseException):
            reply = await reply()
        if isinstance(reply, BaseException):
            raise reply
        return ModelResponse(text=str(reply), model_id="fake/model:free", usage={"total_tokens": 10})

    async def health_check(self) -> bool:
        return True


def js(value: Any) -> str:
    return json.dumps(value)


def live_agent(gateway: ModelGateway | None, **kw: Any) -> LiveResearchAgent:
    client = httpx.AsyncClient(transport=httpx.MockTransport(web_handler))
    return LiveResearchAgent(
        search_tool=WebSearchTool(http_client=client),
        fetch_tool=WebFetchTool(http_client=client, url_validator=lambda u: u.startswith("https://")),
        gateway=gateway, tool_gateway=gateway, media_tools={}, **kw,
    )


def request(objective: str = "What is alpha?") -> ResearchRequest:
    return ResearchRequest(objective=objective)


def state_for(req: ResearchRequest) -> ResearchState:
    return ResearchState(request_id=req.id, status="planning", status_history=["planning"])


def plan_reply(*queries: str) -> str:
    return js({"rationale": "cover it", "tasks": [{"section": f"S{i}", "query": q, "focus": "facts"}
                                                  for i, q in enumerate(queries)]})


def rate_limited() -> ModelError:
    return ModelError("OpenRouter error (HTTP 429)", error_code="provider_rate_limit")


async def slow() -> str:
    await asyncio.sleep(5)
    return "{}"


# ---------------------------------------------------------------- JSON parsing


def test_parse_json_handles_fences_and_think_blocks() -> None:
    assert parse_json_object('<think>hmm {"x": 0}</think>```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Sure! {"a": [1, 2]} trailing') == {"a": [1, 2]}
    with pytest.raises(AgentDecisionError):
        parse_json_object("no json here")


# ---------------------------------------------------------------- planner


def test_llm_planner_builds_validated_tasks_and_labels_source() -> None:
    req = request()
    state = state_for(req)
    planner = LlmPlanningAgent(ScriptedLLM(plan=[plan_reply("alpha definition", "alpha history", "alpha definition")]))
    plan = asyncio.run(planner.create_plan(req, state))
    assert [t.tool_input["query"] for t in plan.tasks] == ["alpha definition", "alpha history"]  # deduped
    assert all(t.plan_id == plan.id and t.metadata["planned_by"] == "llm" for t in plan.tasks)
    decision = state.metadata["agent_decisions"][-1]
    assert decision["agent"] == "planner" and decision["source"] == "llm" and decision["model_id"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (rate_limited(), "rate_limit"),
        ("this is not json", "invalid_json"),
        (plan_reply(*[f"q{i}" for i in range(9)]), "invalid_plan"),
        (js({"tasks": [{"query": ""}]}), "invalid_plan"),
        (slow, "timeout"),
    ],
)
def test_planner_falls_back_to_deterministic(reply: Reply, reason: str) -> None:
    req = request()
    state = state_for(req)
    planner = LlmPlanningAgent(ScriptedLLM(plan=[reply]), timeout_seconds=0.2)
    plan = asyncio.run(planner.create_plan(req, state))
    assert plan.tasks and plan.tasks[0].metadata.get("section") == "Fundamentals"  # deterministic plan
    decision = state.metadata["agent_decisions"][-1]
    assert decision["source"] == "deterministic" and decision["fallback_reason"] == reason


def test_planner_replan_uses_gaps_and_skips_attachments() -> None:
    req = request()
    state = state_for(req)
    state.last_evaluation_gaps = ["alpha adoption numbers"]
    llm = ScriptedLLM(plan=[plan_reply("alpha adoption statistics 2026")])
    plan = asyncio.run(LlmPlanningAgent(llm).create_plan(req, state))
    assert [t.tool_input["query"] for t in plan.tasks] == ["alpha adoption statistics 2026"]
    assert state.metadata["agent_decisions"][-1]["replan"] is True


# ---------------------------------------------------------------- evaluator


def _evaluated(reply: Reply, *, iteration: int = 0, evidence: bool = True) -> tuple[Any, ResearchState]:
    req = request()
    state = state_for(req)
    state.iteration_number = iteration
    if evidence:
        state.gathered_evidence = [uuid4()]
    plan = ResearchPlan(request_id=req.id, objective=req.objective, tasks=[])
    task = ResearchTask(plan_id=plan.id, description="d", objective="o", status="completed", result="r")
    plan.tasks.append(task)
    state.completed_task_ids = [task.id]
    agent = LlmEvaluationAgent(ScriptedLLM(eval=[reply]), max_iterations=3, timeout_seconds=0.2)
    return asyncio.run(agent.evaluate(state, plan, {})), state


def test_evaluator_continue_returns_gaps_that_drive_replanning() -> None:
    result, state = _evaluated(js({"decision": "CONTINUE", "confidence": 0.6, "reasoning": "thin",
                                   "scores": {"coverage": 2}, "gaps": ["alpha costs", "alpha risks"]}))
    assert result.decision == "CONTINUE" and result.gaps == ["alpha costs", "alpha risks"]
    decision = state.metadata["agent_decisions"][-1]
    assert decision["source"] == "llm" and decision["scores"] == {"coverage": 2}


@pytest.mark.parametrize(
    ("reply", "reason", "evidence"),
    [
        (rate_limited(), "rate_limit", True),
        ("{broken", "invalid_json", True),
        (js({"decision": "MAYBE"}), "invalid_evaluation", True),
        (js({"decision": "CONTINUE", "gaps": []}), "invalid_evaluation", True),
        (js({"decision": "COMPLETE"}), "invalid_evaluation", False),
        (slow, "timeout", True),
    ],
)
def test_evaluator_falls_back_to_deterministic(reply: Reply, reason: str, evidence: bool) -> None:
    result, state = _evaluated(reply, evidence=evidence)
    decision = state.metadata["agent_decisions"][-1]
    assert decision["source"] == "deterministic" and decision["fallback_reason"] == reason
    assert result.decision in {"COMPLETE", "CONTINUE", "FAILED"}


def test_evaluator_continue_on_last_round_becomes_complete() -> None:
    result, state = _evaluated(js({"decision": "CONTINUE", "gaps": ["more"]}), iteration=2)
    assert result.decision == "COMPLETE"
    assert state.metadata["agent_decisions"][-1]["guard"] == "last_iteration_continue_converted"


# ---------------------------------------------------------------- tool choice


def _task(query: str = "alpha") -> ResearchTask:
    return ResearchTask(plan_id=uuid4(), description="d", objective="alpha facts",
                        tool_input={"query": query}, metadata={"question": "What is alpha?"})


def test_llm_chooses_tools_search_fetch_finish() -> None:
    llm = ScriptedLLM(tool=[js({"action": "search", "query": "alpha explained"}),
                            js({"action": "fetch", "results": [2]}), js({"action": "finish"})])
    result = asyncio.run(live_agent(llm).execute_task(_task()))
    assert result.success
    meta = result.metadata
    assert meta["tool_policy"] == "llm" and meta["queries"] == ["alpha", "alpha explained"]
    assert [s["action"] for s in meta["tool_trace"]] == ["search", "search", "fetch", "finish"]
    assert meta["tool_trace"][2]["urls"] == ["https://example.net/b"]


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        ([rate_limited()], "rate_limit"),
        (["not json"], "invalid_json"),
        ([js({"action": "delete_everything"})], "invalid_action"),
        ([js({"action": "fetch", "results": [99]})], "invalid_action"),
        ([js({"action": "search", "query": "x"}), js({"action": "search", "query": "y"})], "invalid_action"),
        ([slow], "timeout"),
    ],
)
def test_tool_choice_falls_back_to_fixed_sequence(replies: list[Reply], reason: str) -> None:
    agent = live_agent(ScriptedLLM(tool=replies), decision_timeout_seconds=0.2)
    result = asyncio.run(agent.execute_task(_task()))
    assert result.success, result.error  # fixed sequence still produced evidence
    meta = result.metadata
    assert meta["tool_policy"] in {"deterministic", "llm_then_deterministic"}
    assert meta["tool_fallback_reason"] == reason
    assert any(s["source"] == "deterministic" and s["action"] == "fetch" for s in meta["tool_trace"])


def test_partial_llm_tool_run_is_labelled_mixed() -> None:
    llm = ScriptedLLM(tool=[js({"action": "fetch", "results": [1]}), "garbage"])
    result = asyncio.run(live_agent(llm).execute_task(_task()))
    assert result.success and result.metadata["tool_policy"] == "llm_then_deterministic"


def test_agentic_live_mode_widens_tool_timeout_for_decisions() -> None:
    app = ResearchApplication(Settings(_env_file=None, research_mode="live", openrouter_api_key=SecretStr("k")))
    assert app._build_orchestrator().execution_harness is not None
    harness = app._build_orchestrator().execution_harness
    assert harness is not None and harness.limits.max_tool_call_time_seconds >= 85


def test_tool_choice_respects_model_call_budget() -> None:
    def refuse(_: str) -> None:
        raise RuntimeError("Model-call iteration budget exhausted")

    agent = live_agent(ScriptedLLM(tool=[js({"action": "finish"})]), budget_hook=refuse)
    result = asyncio.run(agent.execute_task(_task()))
    assert result.success and result.metadata["tool_fallback_reason"] == "model_budget_exhausted"


# ---------------------------------------------------------------- end to end


def _agentic_app(monkeypatch: pytest.MonkeyPatch, llm: ScriptedLLM) -> ResearchApplication:
    app = ResearchApplication(Settings(_env_file=None, research_mode="live",
                                       openrouter_api_key=SecretStr("test-key")))
    monkeypatch.setattr(app, "_agent_gateway", lambda: llm)
    monkeypatch.setattr(app, "_build_research_agent",
                        lambda: live_agent(llm, budget_hook=app._run_budget_hook))
    return app


def test_end_to_end_llm_plan_evaluate_replan(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLLM(
        plan=[plan_reply("alpha definition"), plan_reply("alpha adoption 2026")],
        eval=[js({"decision": "CONTINUE", "confidence": 0.5, "gaps": ["alpha adoption"]}),
              js({"decision": "COMPLETE", "confidence": 0.8, "reasoning": "covered"})],
        tool=[js({"action": "fetch", "results": [1]}), js({"action": "finish"})],
    )
    app = _agentic_app(monkeypatch, llm)
    resp = asyncio.run(app.submit(ResearchSubmission(objective="What is alpha?")))
    assert resp.status == "completed", resp.error
    report = resp.report or {}
    assert report["agent_mode"] == "llm_with_fallback"
    decisions = report["agent_decisions"]
    assert [(d["agent"], d["source"]) for d in decisions if d["agent"] != "researcher"] == [
        ("planner", "llm"), ("evaluator", "llm"), ("planner", "llm"), ("evaluator", "llm")]
    planners = [d for d in decisions if d["agent"] == "planner"]
    assert planners[1]["replan"] is True and planners[1]["iteration"] == 1
    researchers = [d for d in decisions if d["agent"] == "researcher"]
    assert researchers and researchers[0]["source"] == "llm", str(researchers[0])
    assert "## How this research was run" in str(report["content"])
    assert report["agent_summary"]["evaluator"] == {"llm": 2}


def test_end_to_end_everything_rate_limited_still_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ScriptedLLM(plan=[rate_limited()], eval=[rate_limited()], tool=[rate_limited()])
    app = _agentic_app(monkeypatch, llm)
    resp = asyncio.run(app.submit(ResearchSubmission(objective="What is alpha?")))
    assert resp.status == "completed", resp.error
    decisions = (resp.report or {})["agent_decisions"]
    assert decisions and all(d["source"] == "deterministic" for d in decisions)
    assert {d["fallback_reason"] for d in decisions} == {"rate_limit"}


def test_agent_mode_defaults() -> None:
    assert not ResearchApplication(Settings(_env_file=None)).agentic  # offline default
    key = SecretStr("k")
    assert ResearchApplication(Settings(_env_file=None, research_mode="live", openrouter_api_key=key)).agentic
    assert not ResearchApplication(Settings(_env_file=None, research_mode="live")).agentic  # no key
    assert not ResearchApplication(Settings(_env_file=None, research_mode="live", openrouter_api_key=key,
                                            agent_mode="deterministic")).agentic
