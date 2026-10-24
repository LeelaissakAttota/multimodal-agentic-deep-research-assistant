"""Academic search tool: arXiv / Semantic Scholar / OpenAlex, all network mocked."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.academic_tool import ACADEMIC_TOOL_ID, AcademicSearchTool, _inverted_abstract
from deep_research.tools.tool import ToolRequest

ARXIV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <published>2017-06-12T17:57:34Z</published>
    <title>Attention Is All
      You Need</title>
    <summary>The dominant sequence transduction models are based on recurrent networks.</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
  </entry>
</feed>"""

S2_JSON = {"data": [
    {"title": "Attention is All you Need", "url": "https://www.semanticscholar.org/paper/abc",
     "abstract": "A longer abstract about the Transformer architecture and attention only models.",
     "year": 2017, "authors": [{"name": "Ashish Vaswani"}], "venue": "NeurIPS",
     "externalIds": {"ArXiv": "1706.03762", "DOI": None}, "citationCount": 100000},
    {"title": "BERT", "url": "https://www.semanticscholar.org/paper/bert", "abstract": None, "year": 2018,
     "authors": [], "venue": "", "externalIds": {}, "citationCount": 5},
]}

OPENALEX_JSON = {"results": [
    {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/xyz", "display_name": "Scaling laws",
     "publication_year": 2020, "abstract_inverted_index": {"Scaling": [0], "laws": [1], "matter": [2]},
     "authorships": [{"author": {"display_name": "Jared Kaplan"}}],
     "primary_location": {"landing_page_url": "https://example.org/p", "source": {"display_name": "arXiv"}},
     "cited_by_count": 42},
]}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _router(fail: set[str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    fail = fail or set()

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if "arxiv" in host:
            return httpx.Response(503) if "arxiv" in fail else httpx.Response(200, text=ARXIV_XML)
        if "semanticscholar" in host:
            return httpx.Response(429) if "s2" in fail else httpx.Response(200, json=S2_JSON)
        if "openalex" in host:
            return httpx.Response(500) if "openalex" in fail else httpx.Response(200, json=OPENALEX_JSON)
        return httpx.Response(404)

    return handler


def _run(tool: AcademicSearchTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=ACADEMIC_TOOL_ID, parameters=params)))


def test_definition_is_free_keyless_academic() -> None:
    d = AcademicSearchTool().get_definition()
    assert d.identifier == ACADEMIC_TOOL_ID and d.modality == Modality.ACADEMIC
    assert d.cost_class == "FREE" and not d.auth_required and d.network_required


def test_auto_merges_and_dedupes_providers() -> None:
    result = _run(AcademicSearchTool(http_client=_client(_router()), min_relevance=0.0), query="attention transformer", limit=5)
    assert result.success
    rows = result.output.results
    titles = [r["title"] for r in rows]
    # arXiv + Semantic Scholar copies of the same paper merge into one row (richer abstract kept).
    assert sum("Attention" in t for t in titles) == 1
    attention = next(r for r in rows if "Attention" in r["title"])
    assert "Transformer architecture" in attention["content"]
    assert attention["source_type"] == "academic" and attention["year"] == "2017"
    scaling = next(r for r in rows if r["title"] == "Scaling laws")
    assert scaling["url"] == "https://doi.org/10.1000/xyz" and scaling["content"] == "Scaling laws matter"
    assert result.output.provider == "arxiv,semantic_scholar,openalex"


def test_arxiv_parsing_normalises_title_and_https() -> None:
    result = _run(AcademicSearchTool(provider="arxiv", http_client=_client(_router())), query="attention")
    row = result.output.results[0]
    assert row["title"] == "Attention Is All You Need"
    assert row["url"] == "https://arxiv.org/abs/1706.03762v7"
    assert row["authors"] == "Ashish Vaswani, Noam Shazeer"


def test_one_provider_failing_is_tolerated() -> None:
    result = _run(AcademicSearchTool(http_client=_client(_router({"s2"}))), query="attention")
    assert result.success
    assert result.output.errors == {"semantic_scholar": "http_429"}
    assert result.metadata["provider_errors"] == {"semantic_scholar": "http_429"}


def test_all_providers_failing_is_a_clean_failure() -> None:
    result = _run(AcademicSearchTool(http_client=_client(_router({"arxiv", "s2", "openalex"}))), query="x y")
    assert not result.success
    assert result.metadata["failure_kind"] == "all_providers_failed"


def test_invalid_query_and_provider() -> None:
    assert _run(AcademicSearchTool(http_client=_client(_router())), query=" ").metadata["failure_kind"] == "invalid_input"
    try:
        AcademicSearchTool(provider="scopus")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_limit_is_capped() -> None:
    result = _run(AcademicSearchTool(http_client=_client(_router())), query="attention", limit=1)
    assert len(result.output.results) == 1


def test_inverted_abstract_handles_garbage() -> None:
    assert _inverted_abstract(None) == ""
    assert _inverted_abstract({"b": [1], "a": [0], "x": "bad"}) == "a b"


def test_semantic_scholar_429_logs_and_skips_cleanly(caplog: Any) -> None:
    import logging

    def handler(request: httpx.Request) -> httpx.Response:
        if "semanticscholar" in request.url.host:
            return httpx.Response(429)
        if "openalex" in request.url.host:
            return httpx.Response(200, json={"results": [{"id": "https://openalex.org/W1", "display_name": "T"}]})
        return httpx.Response(200, text='<feed xmlns="http://www.w3.org/2005/Atom"></feed>')

    tool = AcademicSearchTool(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), min_relevance=0.0)
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.academic_tool"):
        result = asyncio.run(tool.execute(ToolRequest(tool_name=ACADEMIC_TOOL_ID, parameters={"query": "rag"})))
    assert result.success
    assert result.metadata["provider_errors"] == {"semantic_scholar": "http_429"}
    assert "Semantic Scholar rate-limited (HTTP 429); skipping" in caplog.text


def test_mixed_items_skip_non_dicts_with_log(caplog: Any) -> None:
    import logging

    body = {"data": ["oops", {"title": "Ok", "url": "https://s2.org/p/1"}]}
    tool = AcademicSearchTool(provider="semantic_scholar", http_client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))), min_relevance=0.0)
    with caplog.at_level(logging.WARNING, logger="deep_research.tools.academic_tool"):
        result = asyncio.run(tool.execute(ToolRequest(tool_name=ACADEMIC_TOOL_ID, parameters={"query": "rag"})))
    assert result.success and len(result.output.results) == 1
    assert "skipped 1 malformed result item" in caplog.text


def test_openalex_query_strips_wildcards():
    from deep_research.tools.academic_tool import _openalex_query

    assert _openalex_query("Does heat cause coral bleaching?") == "Does heat cause coral bleaching"
    assert _openalex_query("coral* reef") == "coral reef"


def test_unrelated_papers_are_dropped_and_ranked():
    from deep_research.tools.source_common import filter_relevant, relevance

    q = "What does recent academic research say about marine heatwaves causing coral bleaching?"
    rows = [{"title": "Does Gravity Care About Electric Charge? A Minimalist Model", "content": "gravity charge"},
            {"title": "Marine heatwaves and coral bleaching", "content": "heatwave driven bleaching of coral reefs"},
            {"title": "Coral reef fish", "content": "fish counts"}]
    kept = filter_relevant(q, rows)
    assert [r["title"] for r in kept] == ["Marine heatwaves and coral bleaching"]
    assert relevance("", "anything") == 1.0


def test_429_is_retried_then_succeeds():
    import asyncio

    import httpx

    from deep_research.tools.source_common import get_with_backoff

    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if calls["n"] < 3 else httpx.Response(200, json={"ok": 1})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await get_with_backoff(c, "https://x.test", base_delay=0.0)

    assert asyncio.run(go()).json() == {"ok": 1} and calls["n"] == 3
