"""User profile + preferences: validation, storage, and how the planner / live agent respect them."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from deep_research.assistant.profile import DEPTHS, UserProfile, apply_profile, load_profile, read_profile, \
    save_profile
from deep_research.assistant.store import AssistantStore
from deep_research.control.service import DeepResearchControl
from deep_research.core.agents.live_research_agent import LiveResearchAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent, source_tasks
from deep_research.core.config import Settings
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchRequest
from deep_research.tools.tool import Tool, ToolRequest, ToolResult


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)


def test_profile_validation() -> None:
    p = UserProfile(saved_topics=[" solar  power ", "solar power", ""], blocked_domains=["https://www.Spam.example/x"])
    assert p.saved_topics == ["solar power"] and p.blocked_domains == ["spam.example"]
    with pytest.raises(ValueError):
        UserProfile(preferred_sources=["nope_tool"])
    with pytest.raises(ValueError):
        UserProfile(blocked_domains=["not a domain"])
    with pytest.raises(ValueError):
        UserProfile(preferred_sources=["academic_search"], blocked_sources=["academic_search"])
    with pytest.raises(ValueError):
        UserProfile(default_depth="extreme")  # type: ignore[arg-type]


def test_profile_round_trip(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / "a.db")
    assert load_profile(store) == UserProfile()
    save_profile(store, {"saved_topics": ["ai safety"], "default_depth": "deep", "blocked_sources": ["reddit_search"]})
    assert load_profile(store).default_depth == "deep"
    assert read_profile(tmp_path / "a.db").saved_topics == ["ai safety"]  # type: ignore[union-attr]
    assert read_profile(tmp_path / "missing.db") is None
    assert not (tmp_path / "missing.db").exists()


def test_corrupt_profile_falls_back_to_defaults(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / "a.db")
    store.put_json("profile", {"preferred_sources": ["bogus"]})
    assert load_profile(store) == UserProfile()


def test_apply_profile_sets_sources_depth_and_domains() -> None:
    base = settings(live_source_tools="all")
    p = UserProfile(preferred_sources=["academic_search"], blocked_sources=["reddit_search"],
                    blocked_domains=["spam.example"], default_depth="quick")
    s = apply_profile(base, p)
    assert "reddit_search" not in s.enabled_source_tools()
    assert s.preferred_sources() == ["academic_search"]
    assert s.blocked_domain_list() == ["spam.example"]
    assert (s.max_research_iterations, s.live_max_search_results, s.live_max_fetch_pages) == DEPTHS["quick"]
    assert apply_profile(base, None) is base


def test_preferred_source_not_enabled_is_ignored() -> None:
    s = apply_profile(settings(live_source_tools="academic_search"), UserProfile(preferred_sources=["reddit_search"]))
    assert s.preferred_sources() == []


def test_planner_adds_preferred_source_task() -> None:
    q = "How does solar energy storage work?"
    assert [t.assigned_tool for t in source_tasks(q, ["academic_search", "hackernews_search"])] == []
    tasks = source_tasks(q, ["academic_search", "hackernews_search"], ["hackernews_search"])
    assert [t.assigned_tool for t in tasks] == ["hackernews_search"]
    # already routed -> not duplicated
    routed = source_tasks("latest research papers on solar", ["academic_search"], ["academic_search"])
    assert [t.assigned_tool for t in routed] == ["academic_search"]


def test_deterministic_planner_respects_blocked_and_preferred() -> None:
    s = apply_profile(settings(live_source_tools="all"),
                      UserProfile(preferred_sources=["hackernews_search"], blocked_sources=["academic_search"]))
    agent = DeterministicPlanningAgent(s.enabled_source_tools(), s.preferred_sources())
    req = ResearchRequest(objective="latest research papers on solar storage")
    plan = asyncio.run(agent.create_plan(req, ResearchState(request_id=req.id)))
    tools = {t.assigned_tool for t in plan.tasks}
    assert "academic_search" not in tools and "hackernews_search" in tools


class _Search(Tool):
    @property
    def name(self) -> str:
        return "web_search"

    async def health_check(self) -> bool:
        return True

    def get_definition(self) -> Any:
        return None

    async def execute(self, request: ToolRequest) -> ToolResult:
        return ToolResult(success=True, tool_name="web_search", output={"results": [
            {"url": "https://spam.example/a", "title": "A", "snippet": "x"},
            {"url": "https://sub.spam.example/b", "title": "B", "snippet": "x"},
            {"url": "https://good.example/c", "title": "C", "snippet": "x"}]})


def test_live_agent_drops_blocked_domains() -> None:
    agent = LiveResearchAgent(search_tool=_Search(), fetch_tool=_Search(), blocked_domains=["www.spam.example"])
    assert agent.is_blocked("https://sub.spam.example/x") and not agent.is_blocked("https://notspam.example")
    from deep_research.core.agents.live_research_agent import _ToolRun

    run = _ToolRun()
    asyncio.run(agent._search(run, "q"))
    assert [h["url"] for h in run.hits] == ["https://good.example/c"]


def test_control_applies_saved_profile(tmp_path: Path) -> None:
    db = tmp_path / "assistant.db"
    control = DeepResearchControl(settings(), config_dir=tmp_path / "cfg", assistant_db=db)
    assert control.settings_with_keys().max_research_iterations == settings().max_research_iterations
    assert not db.exists()
    save_profile(AssistantStore(db), UserProfile(default_depth="deep", blocked_sources=["bluesky_search"]))
    s = control.settings_with_keys("live")
    assert s.max_research_iterations == DEPTHS["deep"][0] and "bluesky_search" not in s.enabled_source_tools()
