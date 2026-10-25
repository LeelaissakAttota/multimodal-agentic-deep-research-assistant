"""Keyless Hacker News search via the public Algolia HN API.

``https://hn.algolia.com/api/v1/search?query=...&tags=story`` returns stories
(title, url, points, num_comments, created_at); ``tags=comment,story_<id>``
returns a story's comments. Hits carry ``source_type="social"`` and cite the
HN item page; the story's external link is kept as ``story_url`` only when it
passes the SSRF guard's static checks (this tool never fetches it).
"""

from __future__ import annotations

import logging
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
    filter_relevant,
    get_with_backoff,
    hit,
    ok,
    read_query,
)
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from deep_research.tools.web_tools import is_public_http_url

HACKERNEWS_TOOL_ID = "hackernews_search"
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search"
HN_ITEM_URL = "https://news.ycombinator.com/item?id="
logger = logging.getLogger(__name__)
_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home", ".corp")


def _static_resolver(host: str) -> list[str]:
    """No-DNS resolver for the SSRF guard: refuse local-looking names, treat others as public."""
    lowered = host.lower().rstrip(".")
    if lowered == "localhost" or lowered.endswith(_LOCAL_SUFFIXES) or "." not in lowered:
        return []
    return ["93.184.216.34"]


def safe_external_url(url: Any) -> str:
    """``url`` if it is a public http(s) link by static checks (scheme, credentials, IP literals, local names)."""
    text = str(url or "").strip()
    return text if text and is_public_http_url(text, resolver=_static_resolver) else ""


class HackerNewsSearchInput(SourceSearchInput):
    comments: int = 0


class HackerNewsSearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 retry_delay: float | None = None, min_relevance: float = 0.34) -> None:
        self._client = http_client
        self._timeout = timeout
        self._retry_delay = retry_delay if retry_delay is not None else (0.0 if http_client else 1.0)
        self._min_relevance = min_relevance

    @property
    def name(self) -> str:
        return HACKERNEWS_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=HACKERNEWS_TOOL_ID, name="Hacker News",
            description="Search Hacker News stories (and optionally top comments) via the keyless Algolia HN API.",
            modality=Modality.SOCIAL, capabilities=[ToolCapability.SEARCH],
            input_schema=HackerNewsSearchInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="HIGH",
            metadata={"source_type": "social"},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        try:
            query, limit = read_query(request.parameters)
        except ValueError as exc:
            return fail(HACKERNEWS_TOOL_ID, start, str(exc), "invalid_input")
        raw = request.parameters.get("comments", 0)
        n_comments = max(0, min(raw if isinstance(raw, int) and not isinstance(raw, bool) else 0, 5))
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        errors: dict[str, str] = {}
        try:
            try:
                body = await self._get(client, {"query": query, "tags": "story", "hitsPerPage": min(limit * 2, 20)})
                hits = body.get("hits") if isinstance(body, dict) else None
                if not isinstance(hits, list):
                    raise ValueError("no hits list")
            except httpx.HTTPStatusError as exc:
                return _http_failure(start, exc.response.status_code)
            except (httpx.HTTPError, ValueError, TypeError) as exc:
                kind = error_kind(exc)
                logger.warning("Hacker News search failed (%s); falling back to web search", kind)
                return fail(HACKERNEWS_TOOL_ID, start, f"Hacker News search failed ({kind}).", kind)
            rows = [r for r in (_story(h) for h in hits if isinstance(h, dict)) if r]
            rows = filter_relevant(query, rows, self._min_relevance)[:limit]
            for row in rows[: (2 if n_comments else 0)]:
                try:
                    comments = await self._comments(client, row["item_id"], n_comments)
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    errors[row["item_id"]] = error_kind(exc)
                    logger.warning("Hacker News comments for %s failed (%s)", row["item_id"], error_kind(exc))
                    continue
                if comments:
                    row["content"] = clean(row["content"] + " Top comments: " + " | ".join(comments))
        finally:
            if self._client is None:
                await client.aclose()
        return ok(HACKERNEWS_TOOL_ID, start, rows, "hn_algolia", errors)

    async def _get(self, client: httpx.AsyncClient, params: dict[str, Any]) -> Any:
        response = await get_with_backoff(client, HN_SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT},
                                          timeout=self._timeout, base_delay=self._retry_delay)
        return response.json()

    async def _comments(self, client: httpx.AsyncClient, item_id: str, n: int) -> list[str]:
        body = await self._get(client, {"tags": f"comment,story_{item_id}", "hitsPerPage": n})
        out: list[str] = []
        for c in (body.get("hits") or []) if isinstance(body, dict) else []:
            text = clean(c.get("comment_text") or "", 500) if isinstance(c, dict) else ""
            if text:
                out.append(text)
        return out[:n]


def _story(h: dict[str, Any]) -> dict[str, str] | None:
    item_id = str(h.get("objectID") or h.get("story_id") or "")
    title = clean(h.get("title") or h.get("story_title") or "")
    if not item_id.isdigit() or not title:
        return None
    external = safe_external_url(h.get("url"))
    text = clean(h.get("story_text") or "")
    content = text or title
    if external:
        content = f"{content} (links to {external})"
    points, comments = int(h.get("points") or 0), int(h.get("num_comments") or 0)
    return hit(title=title, url=HN_ITEM_URL + item_id, snippet=text or f"{title} — {points} points, {comments} comments",
               content=content, source_type="social", provider="hackernews", item_id=item_id,
               story_url=external, points=str(points), num_comments=str(comments),
               created_at=str(h.get("created_at") or ""), author=str(h.get("author") or ""),
               hn_url=HN_ITEM_URL + item_id)


def _http_failure(start: float, status: int) -> ToolResult:
    kind = {403: "blocked", 429: "rate_limited"}.get(status, f"http_{status}")
    message = f"Hacker News search failed (HTTP {status}); falling back to web search"
    logger.warning(message)
    return fail(HACKERNEWS_TOOL_ID, start, message, kind, http_status=status)
