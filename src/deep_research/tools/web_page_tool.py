"""Read a user-supplied web page URL as an attachment (live mode only).

Wraps :class:`~deep_research.tools.web_tools.WebFetchTool`, so the same SSRF
guard applies: only public http(s) hosts, every redirect re-validated, HTML or
plain text only, size-capped. The page text becomes a primary, cited source
that keeps its real URL.
"""

from __future__ import annotations

import time
from typing import Any

from deep_research.evidence.evidence import Evidence
from deep_research.evidence.source import Source
from deep_research.domain.modality import Modality
from deep_research.tools.definition import ToolCapability, ToolDefinition, ToolInput, ToolOutput
from deep_research.tools.tool import Tool, ToolRequest, ToolResult
from deep_research.tools.web_tools import WebFetchTool, is_public_http_url

WEB_PAGE_TOOL_ID = "web_page_reader"
OFFLINE_NOTE = "Reading a web page URL needs live mode (DEEP_RESEARCH_RESEARCH_MODE=live)."
PAGE_CHARS = 4_000


class WebPageInput(ToolInput):
    page_url: str


class WebPageOutput(ToolOutput):
    url: str
    title: str
    pages: list[dict[str, Any]]


def _fail(start: float, message: str, code: str) -> ToolResult:
    return ToolResult(success=False, error=message, tool_name=WEB_PAGE_TOOL_ID,
                      execution_time_ms=(time.monotonic() - start) * 1000,
                      metadata={"tool_identifier": WEB_PAGE_TOOL_ID, "error_code": code})


def _pages(text: str, size: int = PAGE_CHARS) -> list[dict[str, Any]]:
    """Split long page text into ~size-char sections on sentence/space boundaries."""

    out: list[dict[str, Any]] = []
    text = text.strip()
    while text:
        cut = len(text) if len(text) <= size else max(text.rfind(". ", 0, size) + 1, text.rfind(" ", 0, size), size // 2)
        out.append({"page": len(out) + 1, "text": text[:cut].strip()})
        text = text[cut:].strip()
    return out


class WebPageReaderTool(Tool):
    """Fetch one public web page the user attached, with the SSRF-guarded fetcher."""

    def __init__(self, fetch_tool: WebFetchTool | None = None, *, offline: bool = False,
                 url_validator: Any = is_public_http_url) -> None:
        self._fetch = fetch_tool or WebFetchTool(max_chars=60_000)
        self._validate = url_validator
        self._offline = offline

    @property
    def name(self) -> str:
        return WEB_PAGE_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=WEB_PAGE_TOOL_ID, name="Web Page Reader",
            description="Read a user-supplied public web page as a cited primary source.",
            modality=Modality.WEB, capabilities=[ToolCapability.READ], input_schema=WebPageInput,
            output_schema=WebPageOutput, cost_class="FREE", network_required=True, reliability="MEDIUM",
        )

    async def health_check(self) -> bool:
        return not self._offline

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        url = request.parameters.get("page_url")
        if self._offline:
            return _fail(start, OFFLINE_NOTE, "offline")
        if not isinstance(url, str) or not self._validate(url):
            return _fail(start, "URL must be a public http(s) address.", "blocked_url")
        fetched = await self._fetch.execute(ToolRequest(tool_name="live_web_fetch", parameters={"url": url}))
        if not fetched.success or fetched.output is None:
            return _fail(start, f"Could not read the page: {fetched.error or 'fetch failed'}", "fetch_failed")
        final_url = str(getattr(fetched.output, "url", url))
        text = str(getattr(fetched.output, "text", "") or "")
        if not text.strip():
            return _fail(start, "The page has no readable text.", "empty_page")
        title = str(getattr(fetched.output, "title", "") or "") or request.parameters.get("source_title") or final_url
        pages = _pages(text)
        source = Source(url=final_url, title=title[:200], description="User-attached web page",  # type: ignore[arg-type]
                        metadata={"tool_identifier": WEB_PAGE_TOOL_ID, "attachment": True})
        evidence = Evidence(source_id=source.id, content=text[:2_000], content_type="web_page",
                            metadata={"tool_identifier": WEB_PAGE_TOOL_ID, "url": final_url})
        return ToolResult(
            success=True,
            output={"url": final_url, "title": title, "pages": pages, "page_count": len(pages), "has_text": True,
                    "unit": "section", "truncated": bool(getattr(fetched.output, "truncated", False)),
                    "summary": text[:600], "evidence_ids": [evidence.id], "source_ids": [source.id],
                    "evidence_objects": [evidence], "source_objects": [source]},
            tool_name=WEB_PAGE_TOOL_ID, execution_time_ms=(time.monotonic() - start) * 1000,
            metadata={"tool_identifier": WEB_PAGE_TOOL_ID, "url": final_url},
        )
