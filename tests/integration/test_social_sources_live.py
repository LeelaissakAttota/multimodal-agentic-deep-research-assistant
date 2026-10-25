"""Live smoke tests for the Hacker News and Bluesky source tools (real network).

Skipped unless ``MDRA_LIVE_TESTS=1`` (the default suite stays offline and $0).
"""

from __future__ import annotations

import asyncio
import os

import pytest

from deep_research.tools.bluesky_tool import BLUESKY_TOOL_ID, BlueskySearchTool
from deep_research.tools.hackernews_tool import HACKERNEWS_TOOL_ID, HackerNewsSearchTool
from deep_research.tools.tool import ToolRequest

pytestmark = pytest.mark.skipif(os.environ.get("MDRA_LIVE_TESTS") != "1",
                                reason="live network smoke test; set MDRA_LIVE_TESTS=1")


def test_hackernews_live_smoke() -> None:
    result = asyncio.run(HackerNewsSearchTool().execute(
        ToolRequest(tool_name=HACKERNEWS_TOOL_ID, parameters={"query": "retrieval augmented generation", "limit": 3})))
    assert result.success, result.error
    rows = result.output.results
    assert rows and rows[0]["url"].startswith("https://news.ycombinator.com/item?id=")
    assert rows[0]["points"].isdigit()


def test_bluesky_live_smoke() -> None:
    result = asyncio.run(BlueskySearchTool().execute(
        ToolRequest(tool_name=BLUESKY_TOOL_ID, parameters={"query": "retrieval augmented generation", "limit": 3})))
    if not result.success:
        # A network that needs login without BSKY_* set must degrade cleanly, not crash.
        assert result.metadata["failure_kind"] in ("auth_required", "rate_limited"), result.error
        pytest.skip(result.error or "bluesky unavailable")
    assert result.output.results[0]["url"].startswith("https://bsky.app/profile/")
