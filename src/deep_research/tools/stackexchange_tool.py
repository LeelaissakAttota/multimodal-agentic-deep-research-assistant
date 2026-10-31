"""Keyless Stack Exchange search via ``https://api.stackexchange.com/2.3/search/advanced``.

Queries Stack Overflow by default, or topic sites picked from the question
(stats, math, physics, quant, money, ...) or given as ``site``/``sites``.
The API's throttling fields are honoured: a ``backoff`` of N seconds delays the
next request (or stops the run if it is too long), and the tool stops querying
more sites once ``quota_remaining`` is low. Hits carry ``source_type="social"``
(community Q&A, not authoritative) and cite the question link, kept only if it
is an https Stack Exchange-network URL that passes the SSRF guard's static checks.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition
from deep_research.tools.hackernews_tool import safe_external_url
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

STACKEXCHANGE_TOOL_ID = "stackexchange_search"
SE_SEARCH_URL = "https://api.stackexchange.com/2.3/search/advanced"
DEFAULT_SITE = "stackoverflow"
MAX_SITES = 3
#: Stop querying further sites when the daily quota drops below this.
MIN_QUOTA = 10
#: Longest ``backoff`` we wait for inside one tool call; longer ones stop the run.
MAX_BACKOFF_WAIT = 10.0
logger = logging.getLogger(__name__)
_SITE = re.compile(r"^[a-z0-9.-]{2,40}$")
_SE_HOSTS = ("stackoverflow.com", "stackexchange.com", "superuser.com", "serverfault.com", "askubuntu.com",
             "mathoverflow.net", "stackapps.com")
#: Topic site -> keyword stems in the question that route to it (whole words; ``*`` = prefix).
TOPIC_SITES: dict[str, tuple[str, ...]] = {
    "stats": ("statistic*", "regression", "p-value", "bayes*", "variance", "hypothesis", "probabilit*",
              "distribution", "sampling", "confidence interval", "anova", "machine learning"),
    "math": ("math*", "theorem", "proof", "algebra", "calculus", "integral", "matri*", "equation", "topology"),
    "physics": ("physic*", "quantum", "relativity", "thermodynamic*", "particle", "electromagnet*", "mechanics"),
    "quant": ("quant", "quantitative finance", "option pricing", "black-scholes", "derivative pricing",
              "volatility", "portfolio", "trading", "hedg*", "risk model*"),
    "money": ("personal finance", "money", "tax*", "retirement", "mortgage", "invest*", "budget*", "savings",
              "credit card", "401k", "loan*"),
}


def pick_sites(query: str) -> list[str]:
    """Topic sites whose keywords appear in ``query`` (max MAX_SITES), else Stack Overflow."""
    lowered = query.lower()
    picked = [site for site, keys in TOPIC_SITES.items()
              if any(re.search(rf"\b{re.escape(k[:-1])}" if k.endswith("*") else rf"\b{re.escape(k)}(?:s|es)?\b",
                               lowered) for k in keys)]
    return picked[:MAX_SITES] or [DEFAULT_SITE]


def safe_se_link(url: Any) -> str:
    """``url`` only if it is an https Stack Exchange-network link passing the SSRF guard's static checks."""
    link = safe_external_url(url)
    host = httpx.URL(link).host.lower() if link.startswith("https://") else ""
    return link if host and any(host == h or host.endswith("." + h) for h in _SE_HOSTS) else ""


class StackExchangeSearchInput(SourceSearchInput):
    site: str = ""
    sites: list[str] = []
    with_body: bool = False


class _Stop(Exception):
    """Throttled or quota exhausted: stop querying more sites."""


class StackExchangeSearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 retry_delay: float | None = None, min_relevance: float = 0.34,
                 sleep: Callable[[float], Awaitable[None]] | None = None) -> None:
        self._client = http_client
        self._timeout = timeout
        self._retry_delay = retry_delay if retry_delay is not None else (0.0 if http_client else 1.0)
        self._min_relevance = min_relevance
        self._sleep = sleep or asyncio.sleep
        self._backoff_until = 0.0
        self.quota_remaining: int | None = None

    @property
    def name(self) -> str:
        return STACKEXCHANGE_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=STACKEXCHANGE_TOOL_ID, name="Stack Exchange",
            description="Search Stack Overflow and topic Stack Exchange sites (stats, math, physics, quant, money); keyless.",
            modality=Modality.SOCIAL, capabilities=[ToolCapability.SEARCH],
            input_schema=StackExchangeSearchInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="HIGH",
            metadata={"source_type": "social"},
        )

    async def health_check(self) -> bool:
        return True

    def _sites(self, params: dict[str, Any], query: str) -> list[str] | None:
        raw = params.get("sites") or ([params["site"]] if params.get("site") else [])
        if not isinstance(raw, list):
            return None
        wanted = [str(s).strip().lower() for s in raw if str(s).strip()]
        if any(not _SITE.match(s) for s in wanted):
            return None
        return list(dict.fromkeys(wanted))[:MAX_SITES] or pick_sites(query)

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        try:
            query, limit = read_query(request.parameters)
        except ValueError as exc:
            return fail(STACKEXCHANGE_TOOL_ID, start, str(exc), "invalid_input")
        sites = self._sites(request.parameters, query)
        if sites is None:
            return fail(STACKEXCHANGE_TOOL_ID, start, "Invalid Stack Exchange site name.", "invalid_input")
        with_body = request.parameters.get("with_body") is True
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        errors: dict[str, str] = {}
        rows: list[dict[str, str]] = []
        attempted: list[str] = []
        try:
            for site in sites:
                attempted.append(site)
                try:
                    rows.extend(await self._site(client, query, site, limit, with_body))
                except _Stop as exc:
                    errors[site] = str(exc)
                    break
                except httpx.HTTPStatusError as exc:
                    errors[site] = _status_kind(exc.response)
                    logger.warning("Stack Exchange %s failed (%s)", site, errors[site])
                except (httpx.HTTPError, ValueError, TypeError) as exc:
                    errors[site] = error_kind(exc)
                    logger.warning("Stack Exchange %s failed (%s)", site, errors[site])
        finally:
            if self._client is None:
                await client.aclose()
        succeeded = [s for s in sites[:len(attempted)] if s not in errors]
        if not rows and errors and not succeeded:
            kind = next(iter(errors.values()))
            message = f"Stack Exchange search failed ({kind}); falling back to web search"
            logger.warning(message)
            return fail(STACKEXCHANGE_TOOL_ID, start, message, kind, provider_errors=errors,
                        quota_remaining=self.quota_remaining)
        results = filter_relevant(query, rows, self._min_relevance)
        result = ok(STACKEXCHANGE_TOOL_ID, start, results[:limit], ",".join(succeeded) or "stackexchange", errors)
        result.metadata["quota_remaining"] = self.quota_remaining
        return result

    async def _site(self, client: httpx.AsyncClient, query: str, site: str, limit: int,
                    with_body: bool) -> list[dict[str, str]]:
        await self._respect_backoff()
        if self.quota_remaining is not None and self.quota_remaining < MIN_QUOTA:
            logger.warning("Stack Exchange quota low (%s left); skipping %s", self.quota_remaining, site)
            raise _Stop("quota_low")
        params: dict[str, Any] = {"q": query, "site": site, "order": "desc", "sort": "relevance",
                                  "pagesize": min(limit * 2, 20)}
        if with_body:
            params["filter"] = "withbody"
        response = await get_with_backoff(client, SE_SEARCH_URL, params=params, headers={"User-Agent": USER_AGENT},
                                          timeout=self._timeout, base_delay=self._retry_delay)
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise ValueError("no items list")
        self._note_throttle(body, site)
        return [r for r in (_question(i, site) for i in body["items"] if isinstance(i, dict)) if r]

    def _note_throttle(self, body: dict[str, Any], site: str) -> None:
        quota = body.get("quota_remaining")
        if isinstance(quota, int):
            self.quota_remaining = quota
            if quota < MIN_QUOTA:
                logger.warning("Stack Exchange quota nearly exhausted (%s requests left today)", quota)
        backoff = body.get("backoff")
        if isinstance(backoff, (int, float)) and backoff > 0:
            logger.warning("Stack Exchange asked to back off %ss after %s", backoff, site)
            self._backoff_until = max(self._backoff_until, time.monotonic() + float(backoff))

    async def _respect_backoff(self) -> None:
        wait = self._backoff_until - time.monotonic()
        if wait <= 0:
            return
        if wait > MAX_BACKOFF_WAIT:
            logger.warning("Stack Exchange backoff %.0fs too long; stopping and falling back to web search", wait)
            raise _Stop("backoff")
        logger.info("Stack Exchange: waiting %.1fs (API backoff)", wait)
        await self._sleep(wait)


def _status_kind(response: httpx.Response) -> str:
    try:
        name = str(response.json().get("error_name") or "")
    except (ValueError, AttributeError):
        name = ""
    if response.status_code == 429 or name == "throttle_violation":
        return "rate_limited"
    return "blocked" if response.status_code == 403 else f"http_{response.status_code}"


def _question(item: dict[str, Any], site: str) -> dict[str, str] | None:
    link = safe_se_link(item.get("link"))
    title = clean(item.get("title") or "")
    if not link or not title:
        return None
    created = item.get("creation_date")
    created_at = (datetime.fromtimestamp(created, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                  if isinstance(created, int) and created > 0 else "")
    body = clean(item.get("body") or "")
    tags = [str(t) for t in item.get("tags") or [] if isinstance(t, str)]
    answered = bool(item.get("is_answered"))
    score, answers = int(item.get("score") or 0), int(item.get("answer_count") or 0)
    summary = f"{title} — score {score}, {answers} answers{' (answered)' if answered else ''}"
    return hit(title=title, url=link, snippet=body[:400] or summary,
               content=f"{body or title} Tags: {', '.join(tags)}" if tags else (body or title),
               source_type="social", provider="stackexchange", site=site, score=str(score),
               answer_count=str(answers), is_answered=str(answered).lower(), creation_date=created_at,
               created_at=created_at, tags=", ".join(tags[:8]))
