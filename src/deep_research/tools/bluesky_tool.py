"""Bluesky post search (``app.bsky.feed.searchPosts``), keyless first, optional login.

Order tried:
1. ``https://public.api.bsky.app/xrpc/`` (the documented public AppView; its CDN
   answers 403 to some datacenter IPs),
2. ``https://api.bsky.app/xrpc/`` (same AppView, unauthenticated),
3. only if both refuse (401/403) and ``BSKY_HANDLE`` + ``BSKY_APP_PASSWORD`` are
   set: ``com.atproto.server.createSession`` on ``https://bsky.social`` and an
   authenticated search there.

Without credentials a refusal is a typed ``auth_required`` failure with a clear
message, so the agent falls back to web search. The password and session
tokens are never logged or returned. Hits carry ``source_type="social"`` and
cite the post's ``https://bsky.app/profile/<handle>/post/<rkey>`` page.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition
from deep_research.tools.reddit_tool import _env
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

BLUESKY_TOOL_ID = "bluesky_search"
PUBLIC_APPVIEWS = ("https://public.api.bsky.app/xrpc/", "https://api.bsky.app/xrpc/")
PDS_BASE = "https://bsky.social/xrpc/"
SEARCH_NSID = "app.bsky.feed.searchPosts"
SESSION_NSID = "com.atproto.server.createSession"
logger = logging.getLogger(__name__)
_HANDLE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_RKEY = re.compile(r"^[A-Za-z0-9._~:-]{1,512}$")
_AUTH_HINT = "set BSKY_HANDLE and BSKY_APP_PASSWORD (an app password) to enable Bluesky search"


class _Refused(Exception):
    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class BlueskySearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 retry_delay: float | None = None, min_relevance: float = 0.34,
                 handle: str | None = None, app_password: str | None = None) -> None:
        self._client = http_client
        self._timeout = timeout
        self._retry_delay = retry_delay if retry_delay is not None else (0.0 if http_client else 1.0)
        self._min_relevance = min_relevance
        # Env is read only for real use (no injected client) so tests stay hermetic.
        use_env = http_client is None
        self._handle = (handle if handle is not None else (_env("BSKY_HANDLE") if use_env else "")).strip()
        self._password = (app_password if app_password is not None else (
            _env("BSKY_APP_PASSWORD") if use_env else "")).strip()
        self._jwt: str | None = None

    @property
    def auth_configured(self) -> bool:
        return bool(self._handle and self._password)

    def __repr__(self) -> str:  # never expose credentials
        return f"BlueskySearchTool(auth_configured={self.auth_configured})"

    @property
    def name(self) -> str:
        return BLUESKY_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=BLUESKY_TOOL_ID, name="Bluesky Posts",
            description="Search public Bluesky posts (keyless public AppView; optional BSKY_HANDLE/BSKY_APP_PASSWORD login).",
            modality=Modality.SOCIAL, capabilities=[ToolCapability.SEARCH],
            input_schema=SourceSearchInput, output_schema=SourceSearchOutput,
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
            return fail(BLUESKY_TOOL_ID, start, str(exc), "invalid_input")
        params: dict[str, Any] = {"q": query, "limit": min(limit * 2, 25), "sort": "top"}
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        try:
            posts, provider = await self._search(client, params)
        except _Refused as exc:
            message = (f"Bluesky search refused (HTTP {exc.status}) after login; check BSKY_HANDLE/BSKY_APP_PASSWORD; "
                       "falling back to web search" if self.auth_configured else
                       f"Bluesky public search refused (HTTP {exc.status}) from this network; {_AUTH_HINT}; "
                       "falling back to web search")
            logger.warning(message)
            return fail(BLUESKY_TOOL_ID, start, message, "blocked" if self.auth_configured else "auth_required",
                        http_status=exc.status)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            kind = "rate_limited" if status == 429 else f"http_{status}"
            message = f"Bluesky search failed (HTTP {status}); falling back to web search"
            logger.warning(message)
            return fail(BLUESKY_TOOL_ID, start, message, kind, http_status=status)
        except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            kind = error_kind(exc)
            logger.warning("Bluesky search failed (%s); falling back to web search", kind)
            return fail(BLUESKY_TOOL_ID, start, f"Bluesky search failed ({kind}).", kind)
        finally:
            if self._client is None:
                await client.aclose()
        rows = [r for r in (_post(p) for p in posts if isinstance(p, dict)) if r]
        return ok(BLUESKY_TOOL_ID, start, filter_relevant(query, rows, self._min_relevance)[:limit], provider)

    async def _search(self, client: httpx.AsyncClient, params: dict[str, Any]) -> tuple[list[Any], str]:
        last = 0
        for base in PUBLIC_APPVIEWS:
            try:
                return _posts(await self._get(client, base + SEARCH_NSID, params, None)), "bluesky_public"
            except httpx.HTTPStatusError as exc:
                last = exc.response.status_code
                if last not in (401, 403):
                    raise
                logger.info("Bluesky %s refused unauthenticated search (HTTP %s)", base, last)
        if not self.auth_configured:
            raise _Refused(last)
        token = await self._session(client)
        try:
            return _posts(await self._get(client, PDS_BASE + SEARCH_NSID, params, token)), "bluesky_auth"
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                self._jwt = None
                raise _Refused(exc.response.status_code) from None
            raise

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any], token: str | None) -> Any:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = await get_with_backoff(client, url, params=params, headers=headers, timeout=self._timeout,
                                          base_delay=self._retry_delay)
        return response.json()

    async def _session(self, client: httpx.AsyncClient) -> str:
        """Access JWT from createSession (cached per tool instance). Raises _Refused on bad credentials."""
        if self._jwt:
            return self._jwt
        response = await client.post(PDS_BASE + SESSION_NSID, timeout=self._timeout,
                                     json={"identifier": self._handle, "password": self._password},
                                     headers={"User-Agent": USER_AGENT})
        if response.status_code in (400, 401, 403):
            logger.warning("Bluesky login failed (HTTP %s); check BSKY_HANDLE/BSKY_APP_PASSWORD", response.status_code)
            raise _Refused(response.status_code)
        response.raise_for_status()
        body = response.json()
        token = str(body.get("accessJwt") or "") if isinstance(body, dict) else ""
        if not token:
            raise ValueError("no accessJwt in createSession response")
        self._jwt = token
        return token


def _posts(body: Any) -> list[Any]:
    posts = body.get("posts") if isinstance(body, dict) else None
    if not isinstance(posts, list):
        raise ValueError("no posts list")
    return posts


def post_url(uri: str, handle: str) -> str:
    """``at://<did>/app.bsky.feed.post/<rkey>`` -> ``https://bsky.app/profile/<handle or did>/post/<rkey>``."""
    parts = uri.removeprefix("at://").split("/")
    if len(parts) != 3 or parts[1] != "app.bsky.feed.post" or not _RKEY.match(parts[2]):
        return ""
    actor = handle if _HANDLE.match(handle) and handle != "handle.invalid" else parts[0]
    if not re.match(r"^(did:[a-z]+:[A-Za-z0-9._:%-]+|[A-Za-z0-9.-]+)$", actor):
        return ""
    return f"https://bsky.app/profile/{actor}/post/{parts[2]}"


def _post(p: dict[str, Any]) -> dict[str, str] | None:
    raw_author, raw_record = p.get("author"), p.get("record")
    author: dict[str, Any] = raw_author if isinstance(raw_author, dict) else {}
    record: dict[str, Any] = raw_record if isinstance(raw_record, dict) else {}
    handle = str(author.get("handle") or "")
    url = post_url(str(p.get("uri") or ""), handle)
    text = clean(record.get("text") or "")
    if not url or not text:
        return None
    name = clean(author.get("displayName") or "", 80)
    who = f"{name} (@{handle})" if name else f"@{handle}"
    return hit(title=f"{who}: {clean(text, 100)}", url=url, snippet=text, content=text, source_type="social",
               provider="bluesky", author=handle, likes=str(p.get("likeCount") or 0),
               reposts=str(p.get("repostCount") or 0), replies=str(p.get("replyCount") or 0),
               created_at=str(record.get("createdAt") or p.get("indexedAt") or ""))
