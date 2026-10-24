"""Keyless Reddit search via the public ``.json`` endpoints.

Searches all of Reddit (or one ``subreddit``) and returns posts as citable hits.
Optionally reads the top comments of the best posts (``comments`` > 0). Reddit
is community discussion, not authoritative evidence; hits carry
``source_type="social"`` so the report can label them as such.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition
from deep_research.tools.source_common import (
    USER_AGENT,
    SourceSearchInput,
    SourceSearchOutput,
    clean,
    error_kind,
    fail,
    hit,
    ok,
    read_query,
)
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

REDDIT_TOOL_ID = "reddit_search"
REDDIT_BASE = "https://www.reddit.com"
logger = logging.getLogger(__name__)
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")



def _post_dicts(children: Any) -> list[dict[str, Any]]:
    """Return the post ``data`` dicts from a listing, skipping null/wrong-shaped entries."""
    if not isinstance(children, list):
        logger.warning("Reddit returned no post list (%s); treating as no results", type(children).__name__)
        return []
    return [c["data"] for c in children if isinstance(c, dict) and isinstance(c.get("data"), dict)]


class RedditSearchInput(SourceSearchInput):
    subreddit: str = ""
    comments: int = 0


class RedditSearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 include_nsfw: bool = False) -> None:
        self._client = http_client
        self._timeout = timeout
        self._nsfw = include_nsfw

    @property
    def name(self) -> str:
        return REDDIT_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=REDDIT_TOOL_ID, name="Reddit Discussions",
            description="Search public Reddit posts and comments (keyless public JSON).",
            modality=Modality.SOCIAL, capabilities=[ToolCapability.SEARCH],
            input_schema=RedditSearchInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="MEDIUM",
            metadata={"source_type": "social"},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        try:
            query, limit = read_query(request.parameters)
        except ValueError as exc:
            return fail(REDDIT_TOOL_ID, start, str(exc), "invalid_input")
        sub = str(request.parameters.get("subreddit") or "").strip().removeprefix("r/")
        if sub and not _SUBREDDIT.match(sub):
            return fail(REDDIT_TOOL_ID, start, "Invalid subreddit name.", "invalid_input")
        raw_comments = request.parameters.get("comments", 0)
        n_comments = max(0, min(raw_comments if isinstance(raw_comments, int) else 0, 5))
        path = f"/r/{sub}/search.json" if sub else "/search.json"
        params: dict[str, Any] = {"q": query, "limit": limit, "sort": "relevance", "raw_json": 1}
        if sub:
            params["restrict_sr"] = "on"
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        errors: dict[str, str] = {}
        try:
            try:
                response = await client.get(REDDIT_BASE + path, params=params,
                                            headers={"User-Agent": USER_AGENT}, timeout=self._timeout)
                response.raise_for_status()
                children = response.json()["data"]["children"]
            except httpx.HTTPStatusError as exc:
                return _blocked_or_failed(start, exc.response.status_code)
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                kind = error_kind(exc)
                logger.warning("Reddit search failed (%s); falling back to web search", kind)
                return fail(REDDIT_TOOL_ID, start, f"Reddit search failed ({kind}).", kind)
            rows = [r for r in (self._post(d) for d in _post_dicts(children)) if r]
            rows = rows[:limit]
            for row in rows[: (2 if n_comments else 0)]:
                try:
                    comments = await self._comments(client, row["permalink"], n_comments)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
                    errors[row["permalink"]] = error_kind(exc)
                    continue
                if comments:
                    row["content"] = clean(row["content"] + " Top comments: " + " | ".join(comments))
        finally:
            if self._client is None:
                await client.aclose()
        return ok(REDDIT_TOOL_ID, start, rows, "reddit_json", errors)

    def _post(self, data: dict[str, Any]) -> dict[str, str] | None:
        permalink = str(data.get("permalink") or "")
        if not permalink.startswith("/r/") or (data.get("over_18") and not self._nsfw):
            return None
        body = clean(data.get("selftext") or "")
        title = clean(data.get("title") or "")
        external = str(data.get("url") or "")
        content = body or title
        if external.startswith("http") and "reddit.com" not in external:
            content = f"{content} (links to {external})"
        return hit(title=title, url=REDDIT_BASE + permalink, snippet=body or title, content=content,
                   source_type="social", provider="reddit", subreddit=str(data.get("subreddit") or ""),
                   score=str(data.get("score") or 0), num_comments=str(data.get("num_comments") or 0),
                   author=str(data.get("author") or ""), permalink=permalink)

    async def _comments(self, client: httpx.AsyncClient, permalink: str, n: int) -> list[str]:
        response = await client.get(f"{REDDIT_BASE}{permalink.rstrip('/')}.json",
                                    params={"limit": n, "sort": "top", "raw_json": 1, "depth": 1},
                                    headers={"User-Agent": USER_AGENT}, timeout=self._timeout)
        response.raise_for_status()
        listing = response.json()[1]["data"]["children"]
        out = []
        for child in listing:
            data = child.get("data") or {}
            if child.get("kind") == "t1" and data.get("body") and data.get("body") not in ("[deleted]", "[removed]"):
                out.append(clean(data["body"], 500))
            if len(out) >= n:
                break
        return out


def _blocked_or_failed(start: float, status: int) -> ToolResult:
    """Typed failure for Reddit HTTP errors; 403/429 are expected blocks, not crashes."""
    if status == 403:
        message = "Reddit blocked request (HTTP 403), likely datacenter IP; falling back to web search"
        kind = "blocked"
    elif status == 429:
        message = "Reddit rate-limited request (HTTP 429); falling back to web search"
        kind = "rate_limited"
    else:
        message = f"Reddit search failed (HTTP {status}); falling back to web search"
        kind = f"http_{status}"
    logger.warning(message)
    return fail(REDDIT_TOOL_ID, start, message, kind, http_status=status)
