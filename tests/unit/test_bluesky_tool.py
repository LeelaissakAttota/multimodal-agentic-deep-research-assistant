"""Bluesky searchPosts tool, all network mocked."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import httpx
import pytest

from deep_research.domain.modality import Modality
from deep_research.tools.bluesky_tool import BLUESKY_TOOL_ID, BlueskySearchTool, post_url
from deep_research.tools.tool import ToolRequest

SECRET = "app-pass-SECRET-1234"
JWT = "jwt-TOKEN-SECRET"
POSTS = {"posts": [
    {"uri": "at://did:plc:abc/app.bsky.feed.post/3kxyz", "indexedAt": "2026-01-01T00:00:00Z",
     "author": {"handle": "alice.bsky.social", "displayName": "Alice"},
     "record": {"text": "Retrieval augmented generation &amp; long context: my notes", "createdAt": "2026-01-01T00:00:00Z"},
     "likeCount": 12, "repostCount": 3, "replyCount": 1},
    {"uri": "at://did:plc:def/app.bsky.feed.post/3kabc", "author": {"handle": "handle.invalid"},
     "record": {"text": "retrieval augmented generation benchmark thread"}},
    {"uri": "at://did:plc:ghi/app.bsky.feed.post/3kq", "author": {"handle": "bob.test"},
     "record": {"text": "my cat is cute"}},
    {"uri": "at://did:plc:x/app.bsky.feed.like/1", "author": {"handle": "c.test"},
     "record": {"text": "retrieval augmented generation like"}},
    {"uri": "at://did:plc:y/app.bsky.feed.post/2", "author": {"handle": "d.test"}, "record": {"text": ""}},
    "junk",
]}


def _client(handler: Callable[[httpx.Request], httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)
    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))


def _run(tool: BlueskySearchTool, **params: Any) -> Any:
    params.setdefault("query", "retrieval augmented generation")
    return asyncio.run(tool.execute(ToolRequest(tool_name=BLUESKY_TOOL_ID, parameters=params)))


def test_definition_is_free_and_keyless() -> None:
    d = BlueskySearchTool(http_client=_client(lambda r: httpx.Response(200), [])).get_definition()
    assert d.modality == Modality.SOCIAL and d.cost_class == "FREE" and not d.auth_required


def test_public_appview_search_parses_posts() -> None:
    calls: list[httpx.Request] = []
    result = _run(BlueskySearchTool(http_client=_client(lambda r: httpx.Response(200, json=POSTS), calls)))
    assert result.success and result.metadata["provider"] == "bluesky_public"
    rows = result.output.results
    assert [r["url"] for r in rows] == ["https://bsky.app/profile/alice.bsky.social/post/3kxyz",
                                        "https://bsky.app/profile/did:plc:def/post/3kabc"]
    first = rows[0]
    assert first["title"].startswith("Alice (@alice.bsky.social): Retrieval augmented generation & long")
    assert (first["likes"], first["reposts"], first["replies"]) == ("12", "3", "1")
    assert first["source_type"] == "social" and first["provider"] == "bluesky"
    assert len(calls) == 1 and calls[0].url.host == "public.api.bsky.app"
    assert calls[0].url.path == "/xrpc/app.bsky.feed.searchPosts" and "Authorization" not in calls[0].headers


def test_public_403_falls_back_to_api_bsky_app_without_login() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403) if request.url.host == "public.api.bsky.app" else httpx.Response(200, json=POSTS)
    result = _run(BlueskySearchTool(http_client=_client(handler, calls)))
    assert result.success and [c.url.host for c in calls] == ["public.api.bsky.app", "api.bsky.app"]


def test_refused_without_credentials_degrades_with_clear_message(caplog: Any) -> None:
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.bluesky_tool"):
        result = _run(BlueskySearchTool(http_client=_client(lambda r: httpx.Response(403), [])))
    assert not result.success and result.metadata["failure_kind"] == "auth_required"
    assert result.metadata["http_status"] == 403
    assert "BSKY_HANDLE and BSKY_APP_PASSWORD" in (result.error or "") and "falling back to web search" in caplog.text


def _auth_handler(calls: list[httpx.Request], *, login_status: int = 200,
                  search_status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host != "bsky.social":
            return httpx.Response(401)
        if request.url.path.endswith("createSession"):
            return httpx.Response(login_status, json={"accessJwt": JWT, "refreshJwt": "r"})
        return httpx.Response(search_status, json=POSTS)
    return handler


def test_login_used_only_after_public_refusal_and_secrets_never_leak(caplog: Any) -> None:
    calls: list[httpx.Request] = []
    tool = BlueskySearchTool(http_client=_client(_auth_handler(calls), calls), handle="me.bsky.social",
                             app_password=SECRET)
    with caplog.at_level(logging.DEBUG):
        result = _run(tool)
        again = _run(tool)
    assert result.success and result.metadata["provider"] == "bluesky_auth" and again.success
    session = [c for c in calls if c.url.path.endswith("createSession")]
    assert len(session) == 1  # JWT cached
    assert session[0].method == "POST" and b"me.bsky.social" in session[0].content
    search = [c for c in calls if c.url.host == "bsky.social" and c.url.path.endswith("searchPosts")]
    assert search[0].headers["Authorization"] == f"Bearer {JWT}"
    blob = caplog.text + repr(tool) + repr(result.model_dump()) + str(result.error)
    assert SECRET not in blob and JWT not in blob


@pytest.mark.parametrize(("login", "search", "kind"), [(401, 200, "blocked"), (200, 403, "blocked"),
                                                      (200, 500, "http_500"), (200, 429, "rate_limited")])
def test_auth_failures_are_typed(login: int, search: int, kind: str) -> None:
    calls: list[httpx.Request] = []
    tool = BlueskySearchTool(http_client=_client(_auth_handler(calls, login_status=login, search_status=search), calls),
                             handle="me.bsky.social", app_password=SECRET)
    result = _run(tool)
    assert not result.success and result.metadata["failure_kind"] == kind and SECRET not in (result.error or "")


def test_retry_on_429_and_errors_do_not_crash() -> None:
    seen: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429) if len(seen) == 1 else httpx.Response(200, json=POSTS)
    assert _run(BlueskySearchTool(http_client=_client(flaky, seen))).success and len(seen) == 2
    bad = _run(BlueskySearchTool(http_client=_client(lambda r: httpx.Response(200, json={"posts": None}), [])))
    assert bad.metadata["failure_kind"] == "malformed_response"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)
    assert _run(BlueskySearchTool(http_client=_client(boom, []))).metadata["failure_kind"] == "network_error"
    assert _run(BlueskySearchTool(http_client=_client(boom, [])), query="").metadata["failure_kind"] == "invalid_input"


def test_env_credentials_are_read_only_without_injected_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BSKY_HANDLE", "env.bsky.social")
    monkeypatch.setenv("BSKY_APP_PASSWORD", SECRET)
    assert BlueskySearchTool().auth_configured
    assert not BlueskySearchTool(http_client=_client(lambda r: httpx.Response(200), [])).auth_configured
    assert SECRET not in repr(BlueskySearchTool())


@pytest.mark.parametrize(("uri", "handle", "url"), [
    ("at://did:plc:a/app.bsky.feed.post/3k", "a.test", "https://bsky.app/profile/a.test/post/3k"),
    ("at://did:plc:a/app.bsky.feed.post/3k", "", "https://bsky.app/profile/did:plc:a/post/3k"),
    ("at://did:plc:a/app.bsky.feed.repost/3k", "a.test", ""),
    ("at://did:plc:a/app.bsky.feed.post/../x", "a.test", ""),
    ("https://evil.example/x", "a.test", ""),
])
def test_post_url(uri: str, handle: str, url: str) -> None:
    assert post_url(uri, handle) == url
