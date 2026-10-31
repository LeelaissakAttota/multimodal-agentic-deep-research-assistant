"""Live research agent: web search -> page fetch -> LLM synthesis with citations.

Used only when ``research_mode == "live"``.  The LLM gateway is optional:
without one (no OpenRouter key) the agent still returns real search
evidence and a snippet-based summary, never fabricated content.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from deep_research.context.context_builder import AgentContext
from deep_research.core.agents.llm_support import AgentDecisionError, ask_json, clean_text
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.attachment_context import select_attachment_sources
from deep_research.core.free_model_guard import ModelCapability
from deep_research.domain.research.research_task import ResearchTask
from deep_research.errors.base import ModelError
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.models.model_gateway import ModelGateway, ModelRequest
from deep_research.security.domains import normalize_domain
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from deep_research.tools.web_tools import WEB_FETCH_TOOL_ID, WEB_SEARCH_TOOL_ID
from deep_research.scale.tracing import traced_tool

LIVE_RESEARCH_TOOL_ID = "live_research"
logger = logging.getLogger(__name__)

_SYNTH_PROMPT = (
    "You are a careful research assistant. Using ONLY the numbered sources below, "
    "write a concise, factual answer to the research objective. Cite sources inline "
    "as [n]. If the sources do not answer the objective, say so explicitly.\n\n"
    "{attachment_note}"
    "Objective: {objective}\n{focus}\nSources:\n{sources}\n\nAnswer:"
)
_ATTACHMENT_NOTE = (
    "Sources marked (attached ...) are documents/images the user provided. They are PRIMARY: use their specific "
    "facts and figures first and cite them; use web sources for context and corroboration.\n\n"
)


_TOOL_PROMPT = """You are a research agent choosing which tool to call next for ONE research task.
Tools:
- {{"action": "search", "query": "<web search query>"}}  (web search; at most {max_searches} searches in total)
- {{"action": "fetch", "results": [<result numbers>]}}  (read full pages; at most {max_fetch} pages in total)
- {{"action": "finish"}}  (enough material gathered; an answer will be written from it)
Return ONLY one JSON object for the single next action.

Research objective: {objective}
{focus}Searches run: {searches}
Pages read: {fetched}
Steps left: {steps_left}
Current search results:
{results}"""


class LiveResearchAgent(ResearchAgent):
    """Search/fetch/synthesize agent.

    With ``tool_gateway`` set, an LLM chooses each next tool call (search with a
    refined query, read specific results, or finish) for up to ``tool_steps``
    decisions inside a ``decision_budget_seconds`` window. Any rate limit, error,
    timeout, invalid JSON, invalid action or budget refusal falls back to the
    fixed sequence (search the task query, read the top ``max_fetch`` results)
    for the rest of the task; the result metadata labels which policy ran.
    """

    def __init__(
        self,
        *,
        search_tool: Tool,
        fetch_tool: Tool,
        gateway: ModelGateway | None = None,
        max_results: int = 5,
        max_fetch: int = 2,
        max_source_chars: int = 3_000,
        max_tokens: int = 800,
        media_tools: dict[str, Tool] | None = None,
        tool_gateway: ModelGateway | None = None,
        tool_steps: int = 3,
        max_searches: int = 2,
        decision_budget_seconds: float = 40.0,
        decision_timeout_seconds: float = 20.0,
        budget_hook: Callable[[str], None] | None = None,
        source_tools: dict[str, Tool] | None = None,
        blocked_domains: list[str] | None = None,
        preamble: str = "",
    ) -> None:
        self.preamble = preamble  # writer identity + lessons, prepended to the synthesis prompt
        self.source_tools: dict[str, Tool] = dict(source_tools or {})
        self.blocked_domains = [n for n in (normalize_domain(d) for d in blocked_domains or []) if n]
        self.tool_gateway = tool_gateway
        self.tool_steps = tool_steps
        self.max_searches = max_searches
        self.decision_budget_seconds = decision_budget_seconds
        self.decision_timeout_seconds = decision_timeout_seconds
        self.budget_hook = budget_hook
        self.media_tools: dict[str, Tool] = dict(media_tools) if media_tools is not None else _default_media_tools()
        self.search_tool = search_tool
        self.fetch_tool = fetch_tool
        self.gateway = gateway
        self.max_results = max_results
        self.max_fetch = max_fetch
        self.max_source_chars = max_source_chars
        self.max_tokens = max_tokens

    async def execute_task(self, task: ResearchTask, context: AgentContext | None = None) -> ToolResult:
        start = time.monotonic()
        params = task.tool_input or {}
        if task.assigned_tool and task.assigned_tool in self.media_tools:
            return await traced_tool(self.media_tools[task.assigned_tool],
                ToolRequest(tool_name=task.assigned_tool, parameters=params)
            )
        query = params.get("query") if isinstance(params.get("query"), str) else None
        query = (query or task.objective or task.description).strip()
        objective = str(task.metadata.get("question") or query)
        att_sources, att_evidence = select_attachment_sources(task.metadata.get("attachment_context"), objective)

        focus = str(task.metadata.get("focus") or "")
        source_fallback: str | None = None
        if task.assigned_tool and task.assigned_tool in self.source_tools:
            sourced = await self._source_task(task.assigned_tool, params, query=query, objective=objective,
                                              focus=focus, att=(att_sources, att_evidence), start=start)
            if sourced.success:
                return sourced
            # Honest fallback: the public source failed or was empty, so research the same query on the web.
            source_fallback = str(sourced.metadata.get("failure_kind") or sourced.error or "source_failed")
            logger.warning("Source tool %s unavailable (%s); falling back to web search for this task",
                           task.assigned_tool, source_fallback)
        run = _ToolRun()
        if self.tool_gateway is not None:
            await self._llm_tool_loop(run, objective=objective, query=query, focus=focus, start=start)
        if not run.searches:
            await self._search(run, query)
        search = run.last_search
        # Pages the agent read come first so they survive the source cap.
        hits = sorted(run.hits, key=lambda h: h["url"] not in run.fetched)[: self.max_results]
        if not hits and search is not None and not search.success and not att_sources:
            return ToolResult(success=False, error=search.error or "Web search failed", tool_name=LIVE_RESEARCH_TOOL_ID,
                              execution_time_ms=_ms(start),
                              metadata={**search.metadata, "stage": "search", **run.labels()})
        if not hits and not att_sources:
            return ToolResult(success=False, error=f"No web results for: {query}", tool_name=LIVE_RESEARCH_TOOL_ID,
                              execution_time_ms=_ms(start),
                              metadata={"failure_kind": "no_results", "stage": "search", **run.labels()})

        web_sources, web_evidence = await self._web_sources(
            hits, query, prefetched=run.pages if run.fetched else None
        )
        sources = [*att_sources, *web_sources]
        evidence = [*att_evidence, *web_evidence]
        if not evidence:
            return ToolResult(success=False, error="Web results contained no usable content",
                              tool_name=LIVE_RESEARCH_TOOL_ID, execution_time_ms=_ms(start),
                              metadata={"failure_kind": "no_content"})

        summary, model_id, synth_error = await self._synthesize(objective, sources, evidence, focus=focus,
                                                                has_attachments=bool(att_sources))
        return ToolResult(
            success=True,
            output={
                "summary": summary,
                "evidence_ids": [e.id for e in evidence],
                "source_ids": [s.id for s in sources],
                "evidence_objects": evidence,
                "source_objects": sources,
                "results": hits,
            },
            tool_name=LIVE_RESEARCH_TOOL_ID,
            execution_time_ms=_ms(start),
            metadata={"tool_identifier": LIVE_RESEARCH_TOOL_ID, "query": run.searches[0] if run.searches else query,
                      "queries": list(run.searches), **run.labels(), "model_id": model_id,
                      "synthesis": "llm" if model_id else "snippets", "synthesis_error": synth_error,
                      "attachment_sources": len(att_sources),
                      **({"source_tool": task.assigned_tool, "source_tool_fallback": source_fallback}
                         if source_fallback else {}),
                      "search_error": None if search is None or search.success
                      else (search.error or "search_failed")},
        )

    async def _source_task(self, tool_id: str, params: dict[str, Any], *, query: str, objective: str, focus: str,
                           att: tuple[list[Source], list[Evidence]], start: float) -> ToolResult:
        """Run one public-source tool (academic/video/social/image) and synthesise from its hits."""
        tool_params = {k: v for k, v in params.items() if k != "query"}
        tool_params["query"] = query
        result = await traced_tool(self.source_tools[tool_id], ToolRequest(tool_name=tool_id, parameters=tool_params))
        hits = [h for h in (_get(result.output, "results") or []) if isinstance(h, dict)] if result.success else []
        hits = [h for h in hits if not self.is_blocked(h.get("url"))]
        sources, evidence = source_hits_to_evidence(hits[: self.max_results], tool_id, query,
                                                    max_chars=self.max_source_chars)
        if not evidence:
            return ToolResult(success=False, error=result.error or f"No {tool_id} results for: {query}",
                              tool_name=tool_id, execution_time_ms=_ms(start),
                              metadata={**result.metadata, "failure_kind": result.metadata.get("failure_kind")
                                        or "no_results", "tool_identifier": tool_id})
        att_sources, att_evidence = att
        all_sources, all_evidence = [*att_sources, *sources], [*att_evidence, *evidence]
        summary, model_id, synth_error = await self._synthesize(objective, all_sources, all_evidence, focus=focus,
                                                                has_attachments=bool(att_sources))
        return ToolResult(
            success=True,
            output={"summary": summary, "evidence_ids": [e.id for e in all_evidence],
                    "source_ids": [s.id for s in all_sources], "evidence_objects": all_evidence,
                    "source_objects": all_sources, "results": hits},
            tool_name=LIVE_RESEARCH_TOOL_ID, execution_time_ms=_ms(start),
            metadata={"tool_identifier": LIVE_RESEARCH_TOOL_ID, "source_tool": tool_id, "query": query,
                      "queries": [query], "tool_policy": "planner", "tool_fallback_reason": None,
                      "tool_model_id": None, "tool_trace": [{"action": tool_id, "query": query, "source": "planner"}],
                      "model_id": model_id, "synthesis": "llm" if model_id else "snippets",
                      "synthesis_error": synth_error, "attachment_sources": len(att_sources),
                      "provider": result.metadata.get("provider"),
                      "provider_errors": result.metadata.get("provider_errors") or {}, "search_error": None},
        )

    def is_blocked(self, url: Any) -> bool:
        """True when the URL's host is a blocked domain or one of its subdomains (user preference)."""
        if not self.blocked_domains or not isinstance(url, str):
            return False
        try:
            host = (urlparse(url).hostname or "").lower().rstrip(".").removeprefix("www.")
        except ValueError:
            return False
        domains = [d.rstrip(".") for d in self.blocked_domains]
        return any(d and (host == d or host.endswith("." + d)) for d in domains)

    async def _search(self, run: "_ToolRun", query: str) -> None:
        result = await traced_tool(self.search_tool,
            ToolRequest(tool_name=WEB_SEARCH_TOOL_ID, parameters={"query": query, "limit": self.max_results})
        )
        run.searches.append(query)
        run.last_search = result
        known = {h.get("url") for h in run.hits}
        for hit in list(_get(result.output, "results") or []) if result.success else []:
            if isinstance(hit, dict) and hit.get("url") and hit.get("url") not in known and not self.is_blocked(hit["url"]):
                run.hits.append(hit)
                known.add(hit.get("url"))

    async def _fetch(self, run: "_ToolRun", hit: dict[str, str]) -> None:
        url = hit["url"]
        run.fetched.append(url)
        page = await traced_tool(self.fetch_tool, ToolRequest(tool_name=WEB_FETCH_TOOL_ID, parameters={"url": url}))
        if page.success and _get(page.output, "text"):
            run.pages[url] = (str(_get(page.output, "title") or hit.get("title", "")),
                              str(_get(page.output, "text"))[: self.max_source_chars])

    async def _llm_tool_loop(self, run: "_ToolRun", *, objective: str, query: str, focus: str, start: float) -> None:
        """Let the LLM pick tool calls; on any problem, finish with the fixed sequence."""
        run.policy = "llm"
        # The planner already chose this task's query: run it, then let the LLM steer from the results.
        await self._search(run, query)
        run.trace.append({"action": "search", "query": query, "source": "planner"})
        try:
            for step in range(self.tool_steps):
                if await self._llm_step(run, step=step, objective=objective, query=query, focus=focus, start=start):
                    break
        except AgentDecisionError as exc:
            llm_acted = any(step.get("source") == "llm" for step in run.trace)
            run.policy = "llm_then_deterministic" if llm_acted else "deterministic"
            run.fallback_reason = exc.reason
            run.trace.append({"action": "fallback", "reason": exc.reason, "detail": exc.detail[:160],
                              "source": "deterministic"})
            if not run.searches:
                await self._search(run, query)
                run.trace.append({"action": "search", "query": query, "source": "deterministic"})
        # Fixed rule when the agent read no page: read the top results.
        if not run.fetched and self.max_fetch and run.hits:
            for hit in run.hits[: self.max_fetch]:
                await self._fetch(run, hit)
            run.trace.append({"action": "fetch", "urls": run.fetched[:], "source": "deterministic"})

    async def _llm_step(self, run: "_ToolRun", *, step: int, objective: str, query: str, focus: str,
                        start: float) -> bool:
        """One LLM tool decision. Returns True when the agent chose to finish."""
        assert self.tool_gateway is not None
        remaining = self.decision_budget_seconds - (time.monotonic() - start)
        if remaining <= 1:
            raise AgentDecisionError("decision_budget_exhausted", f"after {step} steps")
        if self.budget_hook is not None:
            try:
                self.budget_hook("tool_choice")
            except Exception as exc:
                raise AgentDecisionError("model_budget_exhausted", str(exc)[:120]) from exc
        reply, model_id = await ask_json(
            self.tool_gateway,
            self._tool_prompt(run, objective=objective, focus=focus, steps_left=self.tool_steps - step),
            max_tokens=400, timeout_seconds=min(self.decision_timeout_seconds, remaining),
        )
        run.model_id = model_id
        action = str(reply.get("action") or "").strip().lower()
        if action == "finish":
            if not run.hits:
                raise AgentDecisionError("invalid_action", "finish before any search")
            run.trace.append({"step": step + 1, "action": "finish", "source": "llm"})
            return True
        if action == "search":
            if len(run.searches) >= self.max_searches:
                raise AgentDecisionError("invalid_action", "search limit reached")
            new_query = clean_text(reply.get("query"), max_len=300) or query
            await self._search(run, new_query)
            run.trace.append({"step": step + 1, "action": "search", "query": new_query, "source": "llm"})
            return False
        if action == "fetch":
            urls = await self._fetch_picks(run, reply.get("results"))
            run.trace.append({"step": step + 1, "action": "fetch", "urls": urls, "source": "llm"})
            return False
        raise AgentDecisionError("invalid_action", f"unknown action {action!r}")

    async def _fetch_picks(self, run: "_ToolRun", picks: Any) -> list[str]:
        if isinstance(picks, int):
            picks = [picks]
        if not isinstance(picks, list) or not picks:
            raise AgentDecisionError("invalid_action", "fetch needs result numbers")
        targets: list[dict[str, str]] = []
        for pick in picks:
            if not isinstance(pick, int) or not 1 <= pick <= len(run.hits):
                raise AgentDecisionError("invalid_action", f"result {pick!r} out of range")
            hit = run.hits[pick - 1]
            if hit["url"] not in run.fetched and hit not in targets:
                targets.append(hit)
        room = self.max_fetch - len(run.fetched)
        if room <= 0 or not targets:
            raise AgentDecisionError("invalid_action", "fetch limit reached or nothing new")
        for hit in targets[:room]:
            await self._fetch(run, hit)
        return [h["url"] for h in targets[:room]]

    def _tool_prompt(self, run: "_ToolRun", *, objective: str, focus: str, steps_left: int) -> str:
        results = "\n".join(
            f"{i}. {h.get('title', '')} ({h.get('url')}){' [read]' if h['url'] in run.fetched else ''}: "
            f"{str(h.get('snippet', ''))[:160]}"
            for i, h in enumerate(run.hits[:12], 1)
        ) or "(none yet - start with a search)"
        return _TOOL_PROMPT.format(
            max_searches=self.max_searches, max_fetch=self.max_fetch, objective=objective,
            focus=f"Focus: {focus}\n" if focus else "", searches=run.searches or "none",
            fetched=len(run.fetched), steps_left=steps_left, results=results,
        )

    async def _web_sources(
        self, hits: list[dict[str, str]], query: str, *, prefetched: dict[str, tuple[str, str]] | None = None
    ) -> tuple[list[Source], list[Evidence]]:
        sources: list[Source] = []
        evidence: list[Evidence] = []
        if prefetched is not None:  # read pages first so they survive the source cap
            hits = sorted(hits, key=lambda h: h["url"] not in prefetched)
        for index, hit in enumerate(hits):
            content, title, fetched = hit.get("snippet", ""), hit.get("title", ""), False
            if prefetched is not None:
                if hit["url"] in prefetched:
                    title, content = prefetched[hit["url"]][0] or title, prefetched[hit["url"]][1]
                    fetched = True
            elif index < self.max_fetch:
                page = await traced_tool(self.fetch_tool,
                    ToolRequest(tool_name=WEB_FETCH_TOOL_ID, parameters={"url": hit["url"]})
                )
                if page.success and _get(page.output, "text"):
                    content = str(_get(page.output, "text"))[: self.max_source_chars]
                    title = str(_get(page.output, "title") or title)
                    fetched = True
            if not content.strip():
                continue
            try:
                source = Source.model_validate({
                    "url": hit["url"], "title": title or None, "description": hit.get("snippet") or None,
                    "metadata": {"tool_identifier": WEB_SEARCH_TOOL_ID, "query": query},
                })
            except ValueError:
                continue
            sources.append(source)
            evidence.append(Evidence(source_id=source.id, content=content, content_type="web_text",
                                     metadata={"fetched": fetched, "url": hit["url"], "provenance": "live_research"}))
        return sources, evidence

    async def _synthesize(self, objective: str, sources: list[Source], evidence: list[Evidence], *,
                          focus: str = "", has_attachments: bool = False) -> tuple[str, str | None, str | None]:
        numbered = "\n\n".join(
            f"[{i}] {s.title or s.url} ({s.url or 'user attachment'})\n{e.content}"
            for i, (s, e) in enumerate(zip(sources, evidence), 1)
        )
        fallback = "\n".join(f"[{i}] {s.title or s.url}: {(s.description or e.content)[:300]}"
                             for i, (s, e) in enumerate(zip(sources, evidence), 1))
        if self.gateway is None:
            return fallback, None, None
        request = ModelRequest(
            prompt=self.preamble + _SYNTH_PROMPT.format(objective=objective, sources=numbered,
                                        focus=f"Focus for this section: {focus}\n" if focus else "",
                                        attachment_note=_ATTACHMENT_NOTE if has_attachments else ""),
            parameters={"max_tokens": self.max_tokens,
                        "required_capability": ModelCapability.TEXT_GENERATION.value},
        )
        try:
            response = await self.gateway.generate(request)
        except ModelError as exc:
            return fallback, None, exc.error_code or "model_error"
        except Exception as exc:  # never let synthesis kill gathered evidence
            return fallback, None, type(exc).__name__
        if not response.text.strip():
            return fallback, None, "empty_response"
        return response.text.strip(), response.model_id, None


class _ToolRun:
    """Mutable per-task record of tool calls and who chose them."""

    def __init__(self) -> None:
        self.searches: list[str] = []
        self.hits: list[dict[str, str]] = []
        self.fetched: list[str] = []
        self.pages: dict[str, tuple[str, str]] = {}
        self.last_search: ToolResult | None = None
        self.policy = "fixed"
        self.fallback_reason: str | None = None
        self.model_id: str | None = None
        self.trace: list[dict[str, Any]] = []

    def labels(self) -> dict[str, Any]:
        return {"tool_policy": self.policy, "tool_fallback_reason": self.fallback_reason,
                "tool_model_id": self.model_id, "tool_trace": list(self.trace)}


#: Hit fields copied into Source.metadata so citations can show what kind of source it is.
_SOURCE_META_FIELDS = ("source_type", "provider", "authors", "year", "venue", "doi", "arxiv_id", "video_id",
                       "subreddit", "score", "image_url", "license", "artist", "caption_kind", "language",
                       "points", "num_comments", "created_at", "story_url", "hn_url", "likes", "reposts",
                       "replies", "author", "site",
                       "answer_count", "is_answered", "creation_date", "tags")


def source_hits_to_evidence(hits: list[dict[str, Any]], tool_id: str, query: str, *,
                            max_chars: int = 3_000) -> tuple[list[Source], list[Evidence]]:
    """Turn public-source tool hits into citable Source + Evidence pairs (hits without text are skipped)."""
    sources: list[Source] = []
    evidence: list[Evidence] = []
    for h in hits:
        content = str(h.get("content") or h.get("snippet") or "").strip()
        if not content or not h.get("url"):
            continue
        meta = {"tool_identifier": tool_id, "query": query,
                **{k: h[k] for k in _SOURCE_META_FIELDS if h.get(k)}}
        try:
            source = Source.model_validate({"url": h["url"], "title": h.get("title") or None,
                                            "description": h.get("snippet") or None, "metadata": meta})
        except ValueError:
            continue
        sources.append(source)
        evidence.append(Evidence(source_id=source.id, content=content[:max_chars],
                                 content_type=f"{meta.get('source_type') or 'web'}_text",
                                 metadata={"fetched": False, "url": h["url"], "provenance": tool_id}))
    return sources, evidence


def _get(output: Any, key: str) -> Any:
    if isinstance(output, dict):
        return output.get(key)
    return getattr(output, key, None)


def _ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _default_media_tools() -> dict[str, Tool]:
    from deep_research.tools.document_tool import DocumentReaderTool
    from deep_research.tools.image_tool import ImageAnalysisTool
    from deep_research.tools.media_tool import MediaAnalysisTool
    from deep_research.tools.pdf_tool import PdfReaderTool

    from deep_research.tools.web_page_tool import WebPageReaderTool

    tools: list[Tool] = [PdfReaderTool(), ImageAnalysisTool(use_settings=False), DocumentReaderTool(),
                         WebPageReaderTool(), MediaAnalysisTool()]
    return {tool.name: tool for tool in tools}
