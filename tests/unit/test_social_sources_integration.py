"""Hacker News + Bluesky wiring: registry, planner routing, citations metadata, dashboard listing."""

from __future__ import annotations

from deep_research.core.agents.live_research_agent import source_hits_to_evidence
from deep_research.core.agents.planning_agent import route_source_tools
from deep_research.core.config import Settings
from deep_research.dashboard import core
from deep_research.tools.registry import SOURCE_TOOL_IDS, SOURCE_TOOLS, build_source_tools, default_registry


def test_registry_and_settings_include_social_sources() -> None:
    assert {"hackernews_search", "bluesky_search"} <= set(SOURCE_TOOL_IDS)
    assert SOURCE_TOOLS["hackernews_search"][0] == SOURCE_TOOLS["bluesky_search"][0] == "social"
    reg = default_registry()
    assert reg.get_definition("hackernews_search").cost_class == "FREE"  # type: ignore[union-attr]
    assert list(build_source_tools(["bluesky_search", "hackernews_search"])) == ["bluesky_search", "hackernews_search"]
    assert Settings(live_source_tools="hackernews_search,bluesky_search").enabled_source_tools() == [
        "hackernews_search", "bluesky_search"]


def test_planner_routes_social_sources_by_keyword() -> None:
    every = list(SOURCE_TOOL_IDS)
    assert route_source_tools("What does Hacker News think about Rust?", every) == ["hackernews_search"]
    assert route_source_tools("Bluesky reactions to the new iPhone", every) == ["bluesky_search"]
    assert route_source_tools("Reddit opinions on mechanical keyboards", every) == ["reddit_search"]
    assert route_source_tools("Bluesky reactions", ["reddit_search"]) == []


def test_social_hits_keep_citation_metadata() -> None:
    hits = [{"title": "Story", "url": "https://news.ycombinator.com/item?id=1", "content": "text body",
             "source_type": "social", "provider": "hackernews", "points": "10", "num_comments": "2",
             "created_at": "2025-01-01", "hn_url": "https://news.ycombinator.com/item?id=1"}]
    sources, evidence = source_hits_to_evidence(hits, "hackernews_search", "q")
    meta = sources[0].metadata
    assert meta["points"] == "10" and meta["num_comments"] == "2" and meta["provider"] == "hackernews" and evidence


def test_dashboard_lists_social_sources() -> None:
    live = {r["name"]: r["state"] for r in core.source_catalog(Settings(), "live")}
    assert live["Hacker News"] == "Active" and live["Bluesky Posts"] == "Active"
    only = {r["name"]: r["state"] for r in core.source_catalog(Settings(live_source_tools="hackernews_search"), "live")}
    assert only["Hacker News"] == "Active" and only["Bluesky Posts"] == "Disabled"
    tools = {t["name"]: t["state"] for t in core.tool_catalog(Settings(), None, "live")}
    assert tools["Hacker News"] == "on" and tools["Bluesky Posts"] == "on"


def test_stackexchange_is_registered_routed_and_listed() -> None:
    every = list(SOURCE_TOOL_IDS)
    assert "stackexchange_search" in every and SOURCE_TOOLS["stackexchange_search"][0] == "social"
    assert route_source_tools("Stack Overflow answers on Python asyncio errors", every) == ["stackexchange_search"]
    live = {r["name"]: r["state"] for r in core.source_catalog(Settings(), "live")}
    assert live["Stack Exchange"] == "Active"
    hits = [{"title": "Q", "url": "https://stackoverflow.com/questions/1", "content": "body", "source_type": "social",
             "site": "stackoverflow", "answer_count": "2", "is_answered": "true", "creation_date": "2024-01-01T00:00:00Z"}]
    meta = source_hits_to_evidence(hits, "stackexchange_search", "q")[0][0].metadata
    assert meta["site"] == "stackoverflow" and meta["answer_count"] == "2" and meta["is_answered"] == "true"
