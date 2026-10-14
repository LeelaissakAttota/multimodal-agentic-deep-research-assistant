"""Live research agent: web search -> page fetch -> LLM synthesis with citations.

Used only when ``research_mode == "live"``.  The LLM gateway is optional:
without one (no OpenRouter key) the agent still returns real search
evidence and a snippet-based summary, never fabricated content.
"""

from __future__ import annotations

import time
from typing import Any

from deep_research.context.context_builder import AgentContext
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.core.free_model_guard import ModelCapability
from deep_research.domain.research.research_task import ResearchTask
from deep_research.errors.base import ModelError
from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.models.model_gateway import ModelGateway, ModelRequest
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from deep_research.tools.web_tools import WEB_FETCH_TOOL_ID, WEB_SEARCH_TOOL_ID

LIVE_RESEARCH_TOOL_ID = "live_research"

_SYNTH_PROMPT = (
    "You are a careful research assistant. Using ONLY the numbered sources below, "
    "write a concise, factual answer to the research objective. Cite sources inline "
    "as [n]. If the sources do not answer the objective, say so explicitly.\n\n"
    "Objective: {objective}\n\nSources:\n{sources}\n\nAnswer:"
)


class LiveResearchAgent(ResearchAgent):
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
    ) -> None:
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
            return await self.media_tools[task.assigned_tool].execute(
                ToolRequest(tool_name=task.assigned_tool, parameters=params)
            )
        query = params.get("query") if isinstance(params.get("query"), str) else None
        query = (query or task.objective or task.description).strip()

        search = await self.search_tool.execute(
            ToolRequest(tool_name=WEB_SEARCH_TOOL_ID, parameters={"query": query, "limit": self.max_results})
        )
        if not search.success:
            return ToolResult(success=False, error=search.error or "Web search failed", tool_name=LIVE_RESEARCH_TOOL_ID,
                              execution_time_ms=_ms(start), metadata={**search.metadata, "stage": "search"})
        hits: list[dict[str, str]] = list(_get(search.output, "results") or [])
        if not hits:
            return ToolResult(success=False, error=f"No web results for: {query}", tool_name=LIVE_RESEARCH_TOOL_ID,
                              execution_time_ms=_ms(start), metadata={"failure_kind": "no_results", "stage": "search"})

        sources: list[Source] = []
        evidence: list[Evidence] = []
        for index, hit in enumerate(hits):
            content, title, fetched = hit.get("snippet", ""), hit.get("title", ""), False
            if index < self.max_fetch:
                page = await self.fetch_tool.execute(
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

        if not evidence:
            return ToolResult(success=False, error="Web results contained no usable content",
                              tool_name=LIVE_RESEARCH_TOOL_ID, execution_time_ms=_ms(start),
                              metadata={"failure_kind": "no_content"})

        summary, model_id, synth_error = await self._synthesize(query, sources, evidence)
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
            metadata={"tool_identifier": LIVE_RESEARCH_TOOL_ID, "query": query, "model_id": model_id,
                      "synthesis": "llm" if model_id else "snippets", "synthesis_error": synth_error},
        )

    async def _synthesize(self, objective: str, sources: list[Source], evidence: list[Evidence]
                          ) -> tuple[str, str | None, str | None]:
        numbered = "\n\n".join(
            f"[{i}] {s.title or s.url} ({s.url})\n{e.content}" for i, (s, e) in enumerate(zip(sources, evidence), 1)
        )
        fallback = "\n".join(f"[{i}] {s.title or s.url}: {(s.description or e.content)[:300]}"
                             for i, (s, e) in enumerate(zip(sources, evidence), 1))
        if self.gateway is None:
            return fallback, None, None
        request = ModelRequest(
            prompt=_SYNTH_PROMPT.format(objective=objective, sources=numbered),
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


def _get(output: Any, key: str) -> Any:
    if isinstance(output, dict):
        return output.get(key)
    return getattr(output, key, None)


def _ms(start: float) -> float:
    return (time.monotonic() - start) * 1000


def _default_media_tools() -> dict[str, Tool]:
    from deep_research.tools.image_tool import ImageAnalysisTool
    from deep_research.tools.pdf_tool import PdfReaderTool

    tools: list[Tool] = [PdfReaderTool(), ImageAnalysisTool(use_settings=False)]
    return {tool.name: tool for tool in tools}
