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
OAUTH_BASE = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
#: Reddit asks API clients for "<platform>:<app id>:<version> (by /u/<user>)"; override with REDDIT_USER_AGENT.
OAUTH_USER_AGENT = "python:deep-research-assistant:0.2 (research assistant; app-only OAuth)"
logger = logging.getLogger(__name__)
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")



def _post_dicts(children: Any) -> list[dict[str, Any]]:
    """Return the post ``data`` dicts from a listing, skipping null/wrong-shaped entries."""
    if not isinstance(children, list):
        logger.warning("Reddit returned no post list (%s); treating as no results", type(children).__name__)
        return []
    return [c["data"] for c in children if isinstance(c, dict) and isinstance(c.get("data"), dict)]


def _env(name: str) -> str:
    """Process env first, then the project's .env (pydantic Settings does not export unknown keys)."""
    import os

    value = os.getenv(name, "")
    if value:
        return value
    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - python-dotenv ships with pydantic-settings
        return ""
    path = os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env"
    try:
        return str(dotenv_values(path).get(name) or "")
    except OSError:
        return ""


class RedditSearchInput(SourceSearchInput):
    subreddit: str = ""
    comments: int = 0


class RedditSearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 include_nsfw: bool = False, client_id: str | None = None, client_secret: str | None = None,
                 user_agent: str | None = None) -> None:
        self._client = http_client
        self._timeout = timeout
        self._nsfw = include_nsfw
        # Official app-only OAuth (client credentials). Env is only read for real use (no injected client), so
        # tests stay hermetic; without credentials the keyless public JSON path is used as before.
        use_env = http_client is None
        self._client_id = client_id if client_id is not None else (_env("REDDIT_CLIENT_ID") if use_env else "")
        self._client_secret = client_secret if client_secret is not None else (
            _env("REDDIT_CLIENT_SECRET") if use_env else "")
        self._ua = user_agent or (_env("REDDIT_USER_AGENT") if use_env else "") or OAUTH_USER_AGENT
        self._token: tuple[str, float] | None = None
        self._auth: str | None = None

    @property
    def oauth_configured(self) -> bool:
        return bool(self._client_id.strip() and self._client_secret.strip())

    async def _access_token(self, client: httpx.AsyncClient) -> str | None:
        """App-only bearer token (cached until shortly before expiry); None on any failure -> public JSON."""
        if not self.oauth_configured:
            return None
        if self._token and self._token[1] > time.monotonic() + 30:
            return self._token[0]
        try:
            response = await client.post(TOKEN_URL, data={"grant_type": "client_credentials"},
                                         auth=(self._client_id.strip(), self._client_secret.strip()),
                                         headers={"User-Agent": self._ua}, timeout=self._timeout)
            response.raise_for_status()
            body = response.json()
            token = str(body.get("access_token") or "")
            if not token:
                raise ValueError("no access_token in response")
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as exc:
            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            logger.warning("Reddit OAuth token request failed (%s); using public JSON", status or error_kind(exc))
            return None
        self._token = (token, time.monotonic() + float(body.get("expires_in") or 3600))
        return token

    @property
    def name(self) -> str:
        return REDDIT_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=REDDIT_TOOL_ID, name="Reddit Discussions",
            description="Search public Reddit posts and comments (app-only OAuth with REDDIT_CLIENT_ID/SECRET, else public JSON).",
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
            token = await self._access_token(client)
            self._auth = token
            try:
                response = await self._search_get(client, path, params, token)
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
        return ok(REDDIT_TOOL_ID, start, rows, "reddit_oauth" if token else "reddit_json", errors)

    async def _search_get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any],
                          token: str | None) -> httpx.Response:
        if token:
            return await client.get(OAUTH_BASE + path.removesuffix(".json"), params=params, timeout=self._timeout,
                                    headers={"User-Agent": self._ua, "Authorization": f"bearer {token}"})
        return await client.get(REDDIT_BASE + path, params=params, headers={"User-Agent": USER_AGENT},
                                timeout=self._timeout)

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
        params: dict[str, Any] = {"limit": n, "sort": "top", "raw_json": 1, "depth": 1}
        if self._auth:
            response = await client.get(f"{OAUTH_BASE}{permalink.rstrip('/')}", params=params, timeout=self._timeout,
                                        headers={"User-Agent": self._ua, "Authorization": f"bearer {self._auth}"})
        else:
            response = await client.get(f"{REDDIT_BASE}{permalink.rstrip('/')}.json", params=params,
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
