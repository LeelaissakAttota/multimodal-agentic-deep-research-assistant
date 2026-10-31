"""Saved profile topics bias the planner only when related to the question."""

from __future__ import annotations

import asyncio
from typing import Any

from deep_research.assistant.profile import UserProfile, apply_profile
from deep_research.core.agents.llm_agents import LlmPlanningAgent
from deep_research.core.agents.planning_agent import DeterministicPlanningAgent, related_topic, topic_tasks
from deep_research.core.config import Settings
from deep_research.domain.research.research_state import ResearchState
from deep_research.domain.research_request import ResearchRequest

Q = "What are the main benefits and risks of small modular nuclear reactors?"


def _plan(topics: list[str], q: str = Q) -> list[dict[str, Any]]:
    req = ResearchRequest(objective=q)
    plan = asyncio.run(DeterministicPlanningAgent(saved_topics=topics).create_plan(req, ResearchState(request_id=req.id)))
    return [{"tool": t.assigned_tool, "input": t.tool_input, "meta": t.metadata, "desc": t.description} for t in plan.tasks]


def test_unrelated_or_empty_topics_leave_plan_unchanged() -> None:
    base = _plan([])
    assert _plan(["Sourdough baking", "Premier League football"]) == base
    assert _plan(["", "   "]) == base
    assert _plan(["nuclear reactors"]) == base  # related but adds no new term


def test_related_topic_adds_one_biased_task() -> None:
    base = _plan([])
    tasks = _plan(["Nuclear fusion energy policy", "Reactor safety regulation in India", "Gardening"])
    assert tasks[: len(base)] == base and len(tasks) == len(base) + 1
    extra = tasks[-1]
    assert extra["tool"] == "web_search" and extra["meta"]["saved_topic"]
    assert extra["input"]["query"].startswith(Q)
    assert extra["meta"]["section"].startswith("Related to ")


def test_best_overlap_wins_and_plural_matching() -> None:
    match = related_topic("Battery recycling economics in Europe", ["battery chemistry", "Recycling batteries economics policy"])
    assert match is not None and match[0] == "Recycling batteries economics policy" and match[1] == ["policy"]
    assert topic_tasks("How do vaccines work?", ["the and with"]) == []


def test_profile_flows_to_settings_and_llm_fallback() -> None:
    settings = apply_profile(Settings(_env_file=None), UserProfile(saved_topics=["Reactor safety | regulation", "AI"]))
    assert settings.saved_topic_list() == ["Reactor safety regulation", "AI"]
    assert apply_profile(Settings(_env_file=None), None).saved_topic_list() == []
    agent = LlmPlanningAgent(None, saved_topics=settings.saved_topic_list())  # type: ignore[arg-type]
    assert isinstance(agent.fallback, DeterministicPlanningAgent)
    assert agent.fallback.saved_topics == ["Reactor safety regulation", "AI"]
