"""Phase 2 edge cases for keyless source tools (Testing Bot). All network mocked."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from deep_research.tools.academic_tool import AcademicSearchTool
from deep_research.tools.commons_image_tool import CommonsImageSearchTool
from deep_research.tools.reddit_tool import RedditSearchTool
from deep_research.tools.source_common import clean, error_kind, read_query
from deep_research.tools.tool import ToolRequest
from deep_research.tools.youtube_tool import YouTubeTranscriptTool, video_id

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(tool: Any, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=tool.name, parameters=params)))


def _status(code: int) -> Handler:
    return lambda r: httpx.Response(code)


def _timeout(r: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("slow", request=r)


# ---------- source_common ----------

@pytest.mark.parametrize("q", ["", "   ", None, 5])
def test_read_query_rejects_bad(q: Any) -> None:
    with pytest.raises(ValueError):
        read_query({"query": q})


def test_read_query_clamps_limit_and_bool() -> None:
    assert read_query({"query": "a", "limit": 999}) == ("a", 10)
    assert read_query({"query": "a", "limit": -3}) == ("a", 1)
    assert read_query({"query": "a", "limit": True}) == ("a", 5)
    assert len(read_query({"query": "x" * 1000})[0]) == 300


def test_clean_unicode_and_tags() -> None:
    assert clean("<b>Ünïcødé</b> &amp; 日本語  \n x") == "Ünïcødé & 日本語 x"


def test_error_kind_mapping() -> None:
    req = httpx.Request("GET", "https://x")
    assert error_kind(httpx.ReadTimeout("t", request=req)) == "timeout"
    assert error_kind(httpx.HTTPStatusError("e", request=req, response=httpx.Response(429))) == "http_429"
    assert error_kind(httpx.ConnectError("c", request=req)) == "network_error"
    assert error_kind(ValueError()) == "malformed_response"


# ---------- academic ----------

@pytest.mark.parametrize("code", [403, 429, 500])
def test_academic_all_fail_status(code: int) -> None:
    res = _run(AcademicSearchTool(http_client=_client(_status(code))), query="rag")
    assert not res.success
    assert res.metadata["provider_errors"] == {p: f"http_{code}" for p in
                                               ("arxiv", "semantic_scholar", "openalex")}


def test_academic_timeout_all() -> None:
    res = _run(AcademicSearchTool(http_client=_client(_timeout)), query="rag")
    assert not res.success and set(res.metadata["provider_errors"].values()) == {"timeout"}


def test_semantic_scholar_429_skipped_others_used() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        if "semanticscholar" in r.url.host:
            return httpx.Response(429)
        if "arxiv" in r.url.host:
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
        return httpx.Response(200, json={"results": [{"doi": "https://doi.org/10.1/x", "display_name": "Ωmega",
                                                      "abstract_inverted_index": None}]})
    res = _run(AcademicSearchTool(http_client=_client(h), min_relevance=0.0), query="rag")
    assert res.success
    assert res.output.errors == {"semantic_scholar": "http_429"}
    assert res.output.results[0]["title"] == "Ωmega"


def test_academic_empty_results() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        if "arxiv" in r.url.host:
            return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"/>')
        return httpx.Response(200, json={})
    res = _run(AcademicSearchTool(http_client=_client(h)), query="nothing")
    assert res.success and res.output.results == []


def test_academic_malformed_json_text() -> None:
    res = _run(AcademicSearchTool(provider="openalex", http_client=_client(
        lambda r: httpx.Response(200, text="not json"))), query="x")
    assert not res.success


def test_academic_missing_fields_skipped() -> None:
    res = _run(AcademicSearchTool(provider="semantic_scholar", http_client=_client(
        lambda r: httpx.Response(200, json={"data": [{"title": "no url"}, {}]}))), query="x")
    assert res.success and res.output.results == []


def test_academic_json_list_body_should_not_crash() -> None:
    res = _run(AcademicSearchTool(provider="semantic_scholar", http_client=_client(
        lambda r: httpx.Response(200, json=["x"]))), query="x")
    assert not res.success


def test_academic_non_dict_item_should_not_crash() -> None:
    res = _run(AcademicSearchTool(provider="semantic_scholar", http_client=_client(
        lambda r: httpx.Response(200, json={"data": ["oops"]}))), query="x")
    assert not res.success


def test_academic_bad_provider() -> None:
    with pytest.raises(ValueError):
        AcademicSearchTool(provider="scopus")


# ---------- reddit ----------

@pytest.mark.parametrize("code", [403, 429, 500])
def test_reddit_status_fail_graceful(code: int) -> None:
    res = _run(RedditSearchTool(http_client=_client(_status(code))), query="rag")
    expected = {403: "blocked", 429: "rate_limited"}.get(code, f"http_{code}")
    assert not res.success and res.metadata["failure_kind"] == expected and res.metadata["http_status"] == code


def test_reddit_timeout() -> None:
    res = _run(RedditSearchTool(http_client=_client(_timeout)), query="rag")
    assert res.metadata["failure_kind"] == "timeout"


def test_reddit_invalid_subreddit() -> None:
    res = _run(RedditSearchTool(http_client=_client(_status(200))), query="x", subreddit="bad name!")
    assert res.metadata["failure_kind"] == "invalid_input"


def test_reddit_empty_and_malformed() -> None:
    ok = _run(RedditSearchTool(http_client=_client(
        lambda r: httpx.Response(200, json={"data": {"children": []}}))), query="x")
    assert ok.success and ok.output.results == []
    bad = _run(RedditSearchTool(http_client=_client(lambda r: httpx.Response(200, text="<html>"))), query="x")
    assert bad.metadata["failure_kind"] == "malformed_response"


def test_reddit_comment_403_recorded_not_fatal() -> None:
    def h(r: httpx.Request) -> httpx.Response:
        if r.url.path.endswith("search.json"):
            return httpx.Response(200, json={"data": {"children": [
                {"kind": "t3", "data": {"title": "Тест 🚀", "permalink": "/r/a/comments/1/t/"}}]}})
        return httpx.Response(403)
    res = _run(RedditSearchTool(http_client=_client(h)), query="x", comments=2)
    assert res.success and res.output.results[0]["title"] == "Тест 🚀"
    assert list(res.output.errors.values()) == ["http_403"]


def test_reddit_null_children_should_not_crash() -> None:
    res = _run(RedditSearchTool(http_client=_client(
        lambda r: httpx.Response(200, json={"data": {"children": None}}))), query="x")
    assert res.success and res.output.results == []
    odd = _run(RedditSearchTool(http_client=_client(
        lambda r: httpx.Response(200, json={"data": {"children": ["x", {"data": "y"}]}}))), query="x")
    assert odd.success and odd.output.results == []


# ---------- youtube ----------

def test_video_id_parsing() -> None:
    assert video_id("https://youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=1") == "dQw4w9WgXcQ"
    assert video_id("not a video") is None


def test_youtube_requires_input_and_valid_id() -> None:
    t = YouTubeTranscriptTool(http_client=_client(_status(200)))
    assert _run(t).metadata["failure_kind"] == "invalid_input"
    assert _run(t, video="https://example.com/x").metadata["failure_kind"] == "invalid_input"


@pytest.mark.parametrize("code", [403, 429, 500])
def test_youtube_watch_page_error_no_transcript(code: int) -> None:
    res = _run(YouTubeTranscriptTool(http_client=_client(_status(code))), video="dQw4w9WgXcQ")
    assert not res.success and res.metadata["failure_kind"] == "no_transcript"
    assert res.metadata["provider_errors"] == {"dQw4w9WgXcQ": f"http_{code}"}


def test_youtube_no_caption_tracks() -> None:
    res = _run(YouTubeTranscriptTool(http_client=_client(
        lambda r: httpx.Response(200, text="<html>no captions</html>"))), video="dQw4w9WgXcQ")
    assert res.metadata["provider_errors"] == {"dQw4w9WgXcQ": "no_captions"}


def test_youtube_search_timeout() -> None:
    res = _run(YouTubeTranscriptTool(http_client=_client(_timeout)), query="how transformers work")
    assert not res.success and res.metadata["failure_kind"] == "timeout"


def test_youtube_search_empty() -> None:
    res = _run(YouTubeTranscriptTool(http_client=_client(lambda r: httpx.Response(200, text=""))),
               query="how transformers work")
    assert not res.success and res.metadata["failure_kind"] == "no_transcript"


# ---------- commons ----------

@pytest.mark.parametrize("code", [403, 429, 500])
def test_commons_status(code: int) -> None:
    res = _run(CommonsImageSearchTool(http_client=_client(_status(code))), query="cat")
    assert res.metadata["failure_kind"] == f"http_{code}"


def test_commons_malformed_and_empty_and_list_body() -> None:
    assert _run(CommonsImageSearchTool(http_client=_client(lambda r: httpx.Response(200, text="x"))),
                query="cat").metadata["failure_kind"] == "malformed_response"
    assert _run(CommonsImageSearchTool(http_client=_client(lambda r: httpx.Response(200, json={}))),
                query="cat").output.results == []
    assert not _run(CommonsImageSearchTool(http_client=_client(lambda r: httpx.Response(200, json=[1]))),
                    query="cat").success


def test_commons_skips_non_image_and_missing_fields() -> None:
    pages = [{"title": "File:a.pdf", "imageinfo": [{"mime": "application/pdf", "descriptionurl": "https://c/a"}]},
             {"title": "File:b.jpg", "imageinfo": []}, "junk",
             {"title": "File:Ünï.jpg", "index": 1,
              "imageinfo": [{"mime": "image/jpeg", "descriptionurl": "https://c/u", "extmetadata": {}}]}]
    res = _run(CommonsImageSearchTool(http_client=_client(
        lambda r: httpx.Response(200, json={"query": {"pages": pages}}))), query="cat")
    assert [r["title"] for r in res.output.results] == ["Ünï.jpg"]
