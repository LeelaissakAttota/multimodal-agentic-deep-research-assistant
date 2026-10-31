"""Keyless academic search: arXiv, Semantic Scholar and OpenAlex.

``provider="auto"`` queries all three, merges the hits and de-duplicates them by
DOI / arXiv id / normalised title. A provider that errors or rate-limits is
skipped (recorded in ``errors``); the tool only fails when every provider fails.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET  # noqa: S405 - arXiv Atom feed, no DTD/entities expanded by ElementTree
from typing import Any, Awaitable, Callable

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

ACADEMIC_TOOL_ID = "academic_search"
ARXIV_URL = "https://export.arxiv.org/api/query"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_URL = "https://api.openalex.org/works"
PROVIDERS = ("arxiv", "semantic_scholar", "openalex")
_ATOM = {"a": "http://www.w3.org/2005/Atom"}

Backend = Callable[[httpx.AsyncClient, str, int], Awaitable[list[dict[str, str]]]]



logger = logging.getLogger(__name__)
_LABELS = {"arxiv": "arXiv", "semantic_scholar": "Semantic Scholar", "openalex": "OpenAlex"}


def _log_provider_error(name: str, exc: Exception) -> None:
    label = _LABELS.get(name, name)
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        logger.warning("%s rate-limited (HTTP 429); skipping this provider", label)
    elif isinstance(exc, httpx.HTTPStatusError):
        logger.warning("%s returned HTTP %s; skipping this provider", label, exc.response.status_code)
    else:
        logger.warning("%s failed (%s); skipping this provider", label, error_kind(exc))


def _items(response: httpx.Response, key: str, provider: str) -> list[dict[str, Any]]:
    """Return the dict items under ``key``; wrong-shaped payloads raise ValueError, stray items are skipped."""
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError(f"{provider}: expected a JSON object, got {type(body).__name__}")
    items = body.get(key) or []
    if not isinstance(items, list):
        raise ValueError(f"{provider}: '{key}' is not a list")
    good = [i for i in items if isinstance(i, dict)]
    if len(good) < len(items):
        logger.warning("%s: skipped %d malformed result item(s)", _LABELS.get(provider, provider),
                       len(items) - len(good))
        if not good:
            raise ValueError(f"{provider}: no well-formed result items")
    return good


class AcademicSearchTool(Tool):
    """Search scholarly literature through free, keyless APIs."""

    def __init__(self, *, provider: str = "auto", http_client: httpx.AsyncClient | None = None,
                 timeout: float = 15.0, retry_delay: float | None = None, min_relevance: float = 0.34) -> None:
        if provider != "auto" and provider not in PROVIDERS:
            raise ValueError(f"Unsupported academic provider: {provider}")
        self.provider = provider
        self._client = http_client
        self._timeout = timeout
        # Back off on 429/503 in real use; injected (test) clients don't sleep.
        self._retry_delay = (0.0 if http_client is not None else 1.0) if retry_delay is None else retry_delay
        self._min_relevance = min_relevance

    @property
    def name(self) -> str:
        return ACADEMIC_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=ACADEMIC_TOOL_ID, name="Academic Search",
            description="Search papers on arXiv, Semantic Scholar and OpenAlex (keyless).",
            modality=Modality.ACADEMIC, capabilities=[ToolCapability.SEARCH],
            input_schema=SourceSearchInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="HIGH",
            metadata={"providers": list(PROVIDERS), "source_type": "academic"},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        try:
            query, limit = read_query(request.parameters)
        except ValueError as exc:
            return fail(ACADEMIC_TOOL_ID, start, str(exc), "invalid_input")
        names = list(PROVIDERS) if self.provider == "auto" else [self.provider]
        backends: dict[str, Backend] = {"arxiv": self._arxiv, "semantic_scholar": self._semantic_scholar,
                                        "openalex": self._openalex}
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        errors: dict[str, str] = {}
        merged: list[dict[str, str]] = []
        try:
            for name in names:
                try:
                    merged.extend(await backends[name](client, query, limit))
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, ET.ParseError) as exc:
                    errors[name] = error_kind(exc)
                    _log_provider_error(name, exc)
        finally:
            if self._client is None:
                await client.aclose()
        if len(errors) == len(names):
            return fail(ACADEMIC_TOOL_ID, start, "All academic providers failed.", "all_providers_failed",
                        provider_errors=errors)
        results = _dedupe(filter_relevant(query, merged, self._min_relevance), limit)
        used = ",".join(n for n in names if n not in errors)
        return ok(ACADEMIC_TOOL_ID, start, results, used, errors)

    async def _get(self, client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> httpx.Response:
        return await get_with_backoff(client, url, params=params, headers={"User-Agent": USER_AGENT},
                                      timeout=self._timeout, base_delay=self._retry_delay)

    async def _arxiv(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await self._get(client, ARXIV_URL, {"search_query": f"all:{query}", "start": 0,
                                                       "max_results": limit})
        root = ET.fromstring(response.text)  # noqa: S314 - see import note
        rows = []
        for entry in root.findall("a:entry", _ATOM):
            url = (entry.findtext("a:id", "", _ATOM) or "").strip().replace("http://", "https://", 1)
            if not url:
                continue
            abstract = entry.findtext("a:summary", "", _ATOM)
            authors = [a.findtext("a:name", "", _ATOM) for a in entry.findall("a:author", _ATOM)]
            rows.append(hit(title=entry.findtext("a:title", "", _ATOM), url=url, snippet=abstract,
                            content=abstract, source_type="academic", provider="arxiv",
                            authors=", ".join(a for a in authors[:6] if a),
                            year=(entry.findtext("a:published", "", _ATOM) or "")[:4],
                            arxiv_id=url.rsplit("/abs/", 1)[-1] if "/abs/" in url else ""))
        return rows

    async def _semantic_scholar(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        response = await self._get(client, SEMANTIC_SCHOLAR_URL, {
            "query": query, "limit": limit,
            "fields": "title,url,abstract,year,authors,venue,externalIds,citationCount"})
        rows = []
        for paper in _items(response, "data", "semantic_scholar"):
            ext = paper.get("externalIds") or {}
            url = paper.get("url") or (f"https://doi.org/{ext['DOI']}" if ext.get("DOI") else "")
            if not url:
                continue
            abstract = paper.get("abstract") or ""
            rows.append(hit(title=paper.get("title") or "", url=url, snippet=abstract, content=abstract,
                            source_type="academic", provider="semantic_scholar",
                            authors=", ".join(str(a.get("name")) for a in (paper.get("authors") or [])[:6]),
                            year=str(paper.get("year") or ""), venue=paper.get("venue") or "",
                            doi=ext.get("DOI") or "", arxiv_id=ext.get("ArXiv") or "",
                            citation_count=str(paper.get("citationCount") or "")))
        return rows

    async def _openalex(self, client: httpx.AsyncClient, query: str, limit: int) -> list[dict[str, str]]:
        # OpenAlex rejects ? and * (wildcards) in stemmed search with HTTP 400.
        response = await self._get(client, OPENALEX_URL, {"search": _openalex_query(query), "per-page": limit})
        rows = []
        for work in _items(response, "results", "openalex"):
            doi = str(work.get("doi") or "")
            location = work.get("primary_location") or {}
            url = doi or location.get("landing_page_url") or str(work.get("id") or "")
            if not url.startswith("http"):
                continue
            abstract = _inverted_abstract(work.get("abstract_inverted_index"))
            authors = [str((a.get("author") or {}).get("display_name") or "")
                       for a in (work.get("authorships") or [])[:6]]
            rows.append(hit(title=work.get("display_name") or work.get("title") or "", url=url, snippet=abstract,
                            content=abstract, source_type="academic", provider="openalex",
                            authors=", ".join(a for a in authors if a),
                            year=str(work.get("publication_year") or ""),
                            venue=str(((location.get("source") or {}).get("display_name")) or ""),
                            doi=doi.replace("https://doi.org/", ""),
                            citation_count=str(work.get("cited_by_count") or "")))
        return rows


def _inverted_abstract(index: Any) -> str:
    """Rebuild an OpenAlex ``abstract_inverted_index`` into plain text."""
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, places in index.items():
        if isinstance(places, list):
            positions.extend((int(p), str(word)) for p in places if isinstance(p, int))
    return " ".join(word for _, word in sorted(positions))


def _key(row: dict[str, str]) -> str:
    if row.get("doi"):
        return "doi:" + row["doi"].lower()
    if row.get("arxiv_id"):
        return "arxiv:" + re.sub(r"v\d+$", "", row["arxiv_id"].lower())
    return "title:" + re.sub(r"[^a-z0-9]", "", clean(row.get("title")).lower())


def _dedupe(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    """Merge duplicates (keep the richest abstract), then interleave providers up to ``limit``."""
    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in rows:
        key = _key(row)
        if key not in by_key:
            by_key[key] = row
            order.append(key)
        elif len(row.get("content", "")) > len(by_key[key].get("content", "")):
            by_key[key] = {**by_key[key], **{k: v for k, v in row.items() if v}}
    buckets: dict[str, list[dict[str, str]]] = {}
    for key in order:
        buckets.setdefault(by_key[key].get("provider", ""), []).append(by_key[key])
    out: list[dict[str, str]] = []
    while len(out) < limit and any(buckets.values()):
        for bucket in buckets.values():
            if bucket and len(out) < limit:
                out.append(bucket.pop(0))
    return out


def _openalex_query(query: str) -> str:
    """Drop wildcard characters OpenAlex refuses in its default (stemmed) search."""
    return " ".join(re.sub(r"[?*]", " ", query).split())
