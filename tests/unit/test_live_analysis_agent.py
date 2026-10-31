"""Live-mode analysis agent: LLM claims validated against excerpts, extractive fallback (offline)."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

from deep_research.core.agents.live_analysis_agent import LiveAnalysisAgent, first_sentence
from deep_research.domain.research.research_plan import ResearchPlan
from deep_research.domain.research.research_state import ResearchState
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse

S1, S2 = str(uuid4()), str(uuid4())


class FakeLLM(ModelGateway):
    def __init__(self, reply: str | BaseException) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        if isinstance(self.reply, BaseException):
            raise self.reply
        return ModelResponse(text=self.reply, model_id="fake/model:free", usage={"total_tokens": 5})

    async def health_check(self) -> bool:
        return True


def live_state() -> ResearchState:
    state = ResearchState(request_id=uuid4())
    state.metadata["live_findings"] = [{"sources": [
        {"source_id": S1, "title": "Alpha", "excerpt": "Short. Alpha particles are helium nuclei emitted in decay."},
        {"source_id": S2, "title": "Beta", "excerpt": "Beta radiation consists of fast electrons or positrons here."},
        {"source_id": S1, "title": "Alpha dup", "excerpt": "ignored duplicate"},
    ]}]
    return state


def plan() -> ResearchPlan:
    return ResearchPlan(request_id=uuid4(), objective="What is radiation?", tasks=[])


def run(agent: LiveAnalysisAgent, state: ResearchState) -> dict:
    return asyncio.run(agent.analyze(state, plan()))


def test_llm_claims_are_linked_to_cited_sources() -> None:
    reply = json.dumps({"claims": [
        {"text": "Alpha particles are helium nuclei.", "sources": [1], "confidence": 0.9},
        {"text": "Beta is electrons.", "sources": [2, 9]},
        {"text": "Uncited claim.", "sources": [7]},
    ], "summary": "Radiation types.", "gaps": ["gamma"]})
    llm = FakeLLM(reply)
    state = live_state()
    result = run(LiveAnalysisAgent(llm), state)
    assert result["analysis_mode"] == "llm"
    texts = [c.text for c in result["claims"]]
    assert texts == ["Alpha particles are helium nuclei.", "Beta is electrons."]
    assert str(result["claims"][0].supported_by[0]) == S1
    assert str(result["claims"][1].supported_by[0]) == S2
    assert result["claims"][0].confidence == 0.9
    assert result["gaps"] == ["gamma"]
    assert "analysis (claim extraction) agent" in llm.prompts[0]
    assert state.metadata["agent_decisions"][-1]["source"] == "llm"


def test_model_failure_falls_back_to_extractive_claims() -> None:
    state = live_state()
    result = run(LiveAnalysisAgent(FakeLLM(ModelError("down"))), state)
    assert result["analysis_mode"] == "extractive"
    assert [c.text for c in result["claims"]] == [
        "Alpha particles are helium nuclei emitted in decay.",
        "Beta radiation consists of fast electrons or positrons here.",
    ]
    assert all(not c.text.startswith("Claim based on evidence") for c in result["claims"])
    assert state.metadata["agent_decisions"][-1]["source"] == "deterministic"


def test_invalid_reply_and_no_model_use_extractive() -> None:
    assert run(LiveAnalysisAgent(FakeLLM("not json")), live_state())["analysis_mode"] == "extractive"
    bad = json.dumps({"claims": [{"text": "x", "sources": [5]}]})
    assert run(LiveAnalysisAgent(FakeLLM(bad)), live_state())["analysis_mode"] == "extractive"
    state = live_state()
    assert run(LiveAnalysisAgent(None), state)["analysis_mode"] == "extractive"
    assert state.metadata["agent_decisions"][-1]["fallback_reason"] == "no_model"


def test_offline_runs_defer_to_deterministic_agent() -> None:
    state = ResearchState(request_id=uuid4())
    result = run(LiveAnalysisAgent(FakeLLM("{}")), state)
    assert "analysis_mode" not in result
    assert result["claims"][0].text.startswith("No evidence gathered")


def test_budget_hook_is_charged_once_per_llm_call() -> None:
    calls: list[str] = []
    reply = json.dumps({"claims": [{"text": "Alpha.", "sources": [1]}]})
    run(LiveAnalysisAgent(FakeLLM(reply), budget_hook=calls.append), live_state())
    assert calls == ["analysis"]


def test_first_sentence_skips_fragments() -> None:
    assert first_sentence("Hi. This sentence has more than six words in it.") == "This sentence has more than six words in it."
    assert first_sentence("too short") == ""
