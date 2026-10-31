"""Agent identity + self-improvement loop: store, reflection, retrieval, pruning, injection, ranking."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from deep_research.learning import LessonStore, SelfImprovement, load_identity
from deep_research.learning.identity import DEFAULT_PATH
from deep_research.learning.reflection import (
    RunSignals,
    classify_topic,
    collect_signals,
    deterministic_lessons,
    reflect,
)


@pytest.fixture()
def store(tmp_path: Path) -> LessonStore:
    return LessonStore(tmp_path / "learning.db", max_lessons=5)


def _signals(**kw: object) -> RunSignals:
    base = dict(run_id="r1", topic="finance", status="completed", elapsed_seconds=40.0, citations=4, claims=4,
                supported_claims=4, credible_citations=1, tool_calls=3)
    base.update(kw)
    return RunSignals(**base)  # type: ignore[arg-type]


def test_identity_file_loads_core_values() -> None:
    ident = load_identity(DEFAULT_PATH)
    assert ident.source.endswith("agent_identity.yaml")
    assert ident.name == "MDRA" and ident.version
    assert any("cite" in v.lower() for v in ident.values)
    assert ident.changelog and ident.changelog[0]["version"] == ident.version


def test_identity_falls_back_to_builtin(tmp_path: Path) -> None:
    ident = load_identity(tmp_path / "missing.yaml")
    assert ident.source == "builtin" and ident.values


def test_core_hash_changes_only_with_core(tmp_path: Path) -> None:
    a = load_identity(tmp_path / "missing.yaml")
    b = load_identity(tmp_path / "missing.yaml")
    assert a.core_hash == b.core_hash


def test_store_add_and_list(store: LessonStore) -> None:
    lesson = store.add("For finance questions, prefer SEC filings and central-bank data.", topic="finance",
                       evidence="score 50; 4 citations", run_id="r1", confidence=0.6)
    assert lesson is not None
    [row] = store.all_lessons()
    assert row.text.startswith("For finance") and row.run_id == "r1" and row.usage_count == 0 and row.enabled


def test_store_rejects_secrets_and_pii(store: LessonStore) -> None:
    assert store.add("Use api_key=sk-abcdefghijklmnopqrstuv for search", topic="general") is None
    assert store.add("Email results to someone@example.com after each run", topic="general") is None
    assert store.add("Call +91 98765 43210 when the run finishes", topic="general") is None
    assert store.add("short", topic="general") is None
    assert store.all_lessons() == []


def test_duplicate_reinforces_instead_of_adding(store: LessonStore) -> None:
    store.add("For finance questions, prefer SEC filings and official statistics.", topic="finance", confidence=0.5)
    store.add("For finance questions prefer SEC filings and official statistics", topic="finance", confidence=0.5)
    [row] = store.all_lessons()
    assert row.confidence == pytest.approx(0.6)


def test_contradiction_lowers_old_lesson(store: LessonStore) -> None:
    store.add("Prefer tool youtube_transcript for finance market questions.", topic="finance", confidence=0.5)
    store.add("Avoid tool youtube_transcript for finance market questions.", topic="finance", confidence=0.5)
    old = next(x for x in store.all_lessons(include_disabled=True) if x.text.startswith("Prefer"))
    assert old.confidence == pytest.approx(0.25)


def test_prune_decays_and_caps(tmp_path: Path) -> None:
    s = LessonStore(tmp_path / "l.db", max_lessons=3, half_life_days=10)
    now = time.time()
    s.add("Old lesson about academic sources for science questions.", topic="science", confidence=0.4,
          now=now - 60 * 86_400)
    for i, word in enumerate(["alpha", "bravo", "charlie", "delta"]):
        s.add(f"Lesson {word} about distinct planning topic {word}{i} queries.", topic="general", confidence=0.5 + i / 10)
    texts = [x.text for x in s.all_lessons()]
    assert len(texts) == 3 and not any(t.startswith("Old") for t in texts)
    assert "Lesson alpha about distinct planning topic alpha0 queries." not in texts


def test_retrieve_prefers_relevant_topic(store: LessonStore) -> None:
    store.add("For finance questions, prefer SEC filings and central-bank data.", topic="finance", confidence=0.6)
    store.add("For health questions, prefer WHO and NIH sources.", topic="health", confidence=0.6)
    found = store.retrieve("How do interest rates affect stock prices?", "finance", k=1)
    assert [x.topic for x in found] == ["finance"]
    store.set_enabled(found[0].id, False)
    assert all(x.topic != "finance" for x in store.retrieve("interest rates stocks", "finance", k=3))


def test_retrieve_uses_embeddings_when_given(store: LessonStore) -> None:
    store.add("Lesson one about planning distinct angles.", topic="general", confidence=0.6)
    store.add("Lesson two about video transcripts.", topic="general", confidence=0.6)

    def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if ("two" in t or t == "q") else [0.0, 1.0] for t in texts]

    assert store.retrieve("q", "general", k=1, embed=embed)[0].text.startswith("Lesson two")


def test_tool_ranking_orders_and_skips(store: LessonStore) -> None:
    for _ in range(4):
        store.record_tool("hackernews", False, 500, "timeout")
        store.record_tool("academic_search", True, 900)
        store.record_tool("bluesky", True, 200)
    store.record_tool("youtube_transcript", False, 100)  # one failure: not enough evidence to skip
    order, skipped = store.rank_tools(["hackernews", "youtube_transcript", "academic_search", "bluesky"])
    assert skipped == ["hackernews"]
    assert order[:2] == ["bluesky", "academic_search"] and "youtube_transcript" in order


def test_tool_error_is_redacted(store: LessonStore) -> None:
    store.record_tool("web_search", False, 10, "401 api_key=supersecretvalue")
    assert "supersecret" not in (store.tool_stats()[0]["last_error"] or "")


def test_topic_classifier() -> None:
    assert classify_topic("Impact of interest rates on stock markets") == "finance"
    assert classify_topic("health effects of intermittent fasting") == "health"
    assert classify_topic("history of the printing press") == "general"


def test_deterministic_lessons_from_signals() -> None:
    lessons = deterministic_lessons(_signals(failed_tools=["hackernews"], credible_citations=1))
    texts = [t for t, _ in lessons]
    assert 1 <= len(texts) <= 3
    assert "hackernews" in texts[0] and any("SEC" in t for t in texts)


def test_score_rewards_better_runs() -> None:
    assert _signals(credible_citations=4).score() > _signals(credible_citations=0, failed_tools=["x"]).score()
    assert _signals(status="failed").score() == 0


class _BrokenGateway:
    async def generate(self, request: object) -> object:
        raise RuntimeError("provider down")


class _Reply:
    def __init__(self, text: str) -> None:
        self.text, self.model_id = text, "m"


class _GoodGateway:
    async def generate(self, request: object) -> _Reply:
        return _Reply('{"lessons": [{"text": "For finance, open SEC filings before news.", "confidence": 0.7}]}')


def test_reflection_falls_back_without_model() -> None:
    lessons, source = asyncio.run(reflect(_signals(), gateway=_BrokenGateway()))
    assert source == "deterministic" and lessons


def test_reflection_uses_llm_when_valid() -> None:
    lessons, source = asyncio.run(reflect(_signals(), gateway=_GoodGateway()))
    assert source == "llm" and lessons == [("For finance, open SEC filings before news.", 0.7)]


def test_collect_signals_uses_report_and_tools() -> None:
    report = {"citations": [{"url": "https://www.sec.gov/x"}, {"url": "https://someblog.example.com"}],
              "claims": [{"supported_by": ["a"]}, {"supported_by": []}]}
    events = [{"tool": "web_search", "success": True, "latency_ms": 10},
              {"tool": "hackernews", "success": False, "latency_ms": 40_000}]
    s = collect_signals("r", "stock prices", "completed", report, None, events,
                        {"agent_decisions": [{"scores": {"coverage": 4, "quality": 2}}], "user_feedback": 1.0})
    assert (s.citations, s.credible_citations, s.coverage) == (2, 1, 0.5)
    assert s.failed_tools == ["hackernews"] and s.slow_tools == ["hackernews"]
    assert s.quality == 3.0 and s.feedback == 1.0


def test_preamble_injects_identity_and_lessons(tmp_path: Path) -> None:
    loop = SelfImprovement(tmp_path / "l.db")
    loop.store.add("For finance questions, prefer SEC filings and central-bank data.", topic="finance")  # type: ignore[union-attr]
    run = loop.before_run("How do interest rates move stock prices?", ["hackernews", "academic_search"])
    assert run.applied and "SEC filings" in run.preamble
    assert "You are MDRA" in run.preamble and "Principles" in run.preamble
    assert loop.store.all_lessons()[0].usage_count == 1  # type: ignore[union-attr]


def test_planner_prompt_starts_with_preamble() -> None:
    from deep_research.core.agents.llm_agents import LlmPlanningAgent
    from deep_research.domain.research.research_state import ResearchState
    from deep_research.domain.research_request import ResearchRequest

    agent = LlmPlanningAgent(_BrokenGateway(), preamble="You are MDRA.\nLESSON X\n\n")  # type: ignore[arg-type]
    request = ResearchRequest(objective="interest rates")
    prompt = agent._prompt(request, ResearchState(request_id=request.id), [])
    assert prompt.startswith("You are MDRA.") and "LESSON X" in prompt


def test_disabled_loop_stores_nothing(tmp_path: Path) -> None:
    loop = SelfImprovement(tmp_path / "l.db", enabled=False)
    run = loop.before_run("q about stocks", ["bluesky"])
    assert loop.store is None and run.tool_order == ["bluesky"] and "You are MDRA" in run.preamble
    assert not (tmp_path / "l.db").exists()


def test_service_learns_then_applies_lessons(tmp_path: Path) -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings

    app = ResearchApplication(Settings(memory_dir=str(tmp_path), self_improvement=True, long_term_memory="off"))
    first = asyncio.run(app.submit(ResearchSubmission(objective="How do interest rates affect stock markets?")))
    learned = first.report["learning"]  # type: ignore[index]
    assert learned["new_lessons"] and learned["applied_lessons"] == []
    second = asyncio.run(app.submit(ResearchSubmission(objective="Why do stock markets fall when interest rates rise?")))
    applied = second.report["learning"]["applied_lessons"]  # type: ignore[index]
    assert applied and applied[0]["id"] in {x["id"] for x in learned["new_lessons"]}
    store = LessonStore(tmp_path / "learning.db")
    assert len(store.runs()) == 2 and store.tool_stats()


def test_identity_page_excludes_failed_runs_from_average(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    from tests.unit.test_dashboard_assistant import APP, _setup

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEP_RESEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    store = LessonStore(tmp_path / "mem" / "learning.db")
    for rid, score, applied, status in [("r1", 80.0, 0, "completed"), ("r2", 90.0, 3, "completed"),
                                        ("r3", 0.0, 3, "failed")]:
        store.record_run(rid, topic="finance", score=score, lessons_applied=applied, metrics={"status": status})
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params.update({"page": "learning"})
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown)
    assert "80 → 90" in text and "1 failed excluded" in text and "#3: failed (not scored)" in text


def test_failed_run_records_failure_reason_without_user_text() -> None:
    meta = {"runtime_failure": {"error_code": "budget_exhausted", "failure_kind": "budget", "message": "secret objective"}}
    sig = collect_signals("r", "How do rates move stocks?", "failed", None, None, [], meta, elapsed_seconds=239.0)
    assert sig.as_dict()["failure_reason"] == "budget:budget_exhausted" and sig.score() == 0.0
    ok = collect_signals("r", "q", "completed", None, None, [], meta)
    assert "failure_reason" not in ok.as_dict()
