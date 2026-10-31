"""Report follow-up chat: grounded answers with citations, refusals, model validation (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from deep_research.assistant.followup import REFUSAL, answer_question, ask_report, build_chat_gateway, \
    extractive_answer, report_passages
from deep_research.assistant.store import AssistantStore, default_db_path
from deep_research.core.config import Settings
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest, ModelResponse

REPORT: dict[str, Any] = {
    "title": "Solar panel efficiency",
    "content": (
        "# Solar panel efficiency\n\n## Recent developments\n"
        "Perovskite tandem cells reached 33.9% efficiency in laboratory tests [1]. "
        "Commercial silicon panels typically convert 20 to 22 percent of sunlight [2].\n\n"
        "## Risks\nPerovskite cells degrade faster when exposed to humidity and heat [1].\n\n"
        "## Sources\n1. Lab record - https://a.example\n2. Panel guide - https://b.example\n"
    ),
    "citations": [
        {"number": 1, "title": "Lab record", "url": "https://a.example", "excerpt": "tandem perovskite record 33.9%"},
        {"number": 2, "title": "Panel guide", "url": "https://b.example", "excerpt": "silicon panels 20-22%"},
    ],
    "claim_scores": [{"text": "Perovskite cells degrade faster under humidity.", "citations": [1], "confidence": 0.7}],
}


class FakeGateway(ModelGateway):
    def __init__(self, text: str = "", error: Exception | None = None) -> None:
        self.text, self.error, self.prompts = text, error, []  # type: ignore[var-annotated]

    async def generate(self, request: ModelRequest) -> ModelResponse:
        self.prompts.append(request.prompt)
        if self.error:
            raise self.error
        return ModelResponse(text=self.text, model_id="test/model:free")

    async def health_check(self) -> bool:
        return True


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_passages_skip_sources_section_and_keep_valid_citations() -> None:
    passages = report_passages(REPORT)
    assert all("https://a.example" not in p.text for p in passages if p.kind == "report")
    assert {p.kind for p in passages} == {"report", "claim", "source"}
    assert any(p.citations == [1] and "33.9%" in p.text for p in passages)


def test_extractive_answer_is_cited_and_grounded() -> None:
    ans = extractive_answer("How efficient are perovskite tandem cells?", REPORT)
    assert ans.supported and ans.mode == "extractive"
    assert "[1]" in ans.text and ans.citations == [1]


def test_unsupported_question_is_refused() -> None:
    ans = extractive_answer("What is the GDP of France in 1850?", REPORT)
    assert not ans.supported and ans.text == REFUSAL and ans.citations == []


def test_empty_report_refuses() -> None:
    assert not extractive_answer("perovskite efficiency", {"content": "", "citations": []}).supported


def test_model_answer_with_valid_citations_is_used() -> None:
    gw = FakeGateway('{"supported": true, "answer": "Tandem cells hit 33.9% [1].", "citations": [1]}')
    ans = run(answer_question("How efficient are perovskite tandem cells?", REPORT, gw))
    assert ans.mode == "llm" and ans.citations == [1] and ans.supported
    assert "Use ONLY the passages" in gw.prompts[0]


def test_model_inventing_a_citation_falls_back_to_extractive() -> None:
    gw = FakeGateway('{"supported": true, "answer": "They hit 50% [7].", "citations": [7]}')
    ans = run(answer_question("How efficient are perovskite tandem cells?", REPORT, gw))
    assert ans.mode == "extractive" and "out-of-report" in ans.reason


def test_model_saying_unsupported_refuses() -> None:
    gw = FakeGateway('{"supported": false, "answer": "", "citations": []}')
    ans = run(answer_question("How efficient are perovskite tandem cells?", REPORT, gw))
    assert not ans.supported and ans.text == REFUSAL


def test_model_error_falls_back_offline() -> None:
    gw = FakeGateway(error=ModelError("rate limited", error_code="provider_rate_limit"))
    ans = run(answer_question("How efficient are perovskite tandem cells?", REPORT, gw))
    assert ans.mode == "extractive" and ans.supported and "rate_limit" in ans.reason


def test_model_not_called_when_report_has_no_support() -> None:
    gw = FakeGateway('{"supported": true, "answer": "x [1]", "citations": [1]}')
    ans = run(answer_question("Who won the 1998 World Cup?", REPORT, gw))
    assert not ans.supported and gw.prompts == []


def test_ask_report_records_history(tmp_path: Any) -> None:
    store = AssistantStore(tmp_path / "a.db")
    run(ask_report(store, "run1", "  perovskite   humidity degrade ", REPORT))
    hist = store.chat_history("run1")
    assert [h["role"] for h in hist] == ["user", "assistant"]
    assert hist[0]["text"] == "perovskite humidity degrade"
    assert hist[1]["meta"]["supported"] is True
    assert store.chat_history("other") == []


def test_no_key_means_offline_gateway() -> None:
    assert build_chat_gateway(Settings(_env_file=None)) is None


def test_default_db_path(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.delenv("DEEP_RESEARCH_ASSISTANT_DB", raising=False)
    assert default_db_path("data") == Path("data") / "assistant.db"
    assert default_db_path() == Path("data") / "assistant.db"
    monkeypatch.setenv("DEEP_RESEARCH_ASSISTANT_DB", str(tmp_path / "x.db"))
    assert default_db_path() == tmp_path / "x.db"
