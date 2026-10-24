import asyncio

import httpx
import pytest

from deep_research.core.attachment_context import attachment_entry, select_attachment_sources
from deep_research.domain.research_request import ResearchAttachment
from deep_research.tools.tool import ToolRequest
from deep_research.tools.web_page_tool import OFFLINE_NOTE, WebPageReaderTool, _pages
from deep_research.tools.web_tools import WebFetchTool

HTML = b"<html><head><title>Reef report</title></head><body><p>Coral reefs bleach when water warms 1C above normal.</p></body></html>"


def public(u: str) -> bool:
    return "127.0.0.1" not in u


def tool(handler) -> WebPageReaderTool:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebPageReaderTool(WebFetchTool(http_client=client, url_validator=public), url_validator=public)


def run(t: WebPageReaderTool, url: str):  # type: ignore[no-untyped-def]
    return asyncio.run(t.execute(ToolRequest(tool_name="web_page_reader", parameters={"page_url": url})))


def test_reads_page_as_cited_source_with_url() -> None:
    res = run(tool(lambda r: httpx.Response(200, content=HTML, headers={"content-type": "text/html"})),
              "https://reefs.example.org/a")
    assert res.success and res.output["title"] == "Reef report"
    assert "bleach" in res.output["pages"][0]["text"]
    entry = attachment_entry("web_page_reader", res.output)
    assert entry and entry["kind"] == "web page" and entry["url"] == "https://reefs.example.org/a"
    sources, evidence = select_attachment_sources([entry], "when do reefs bleach")
    assert str(sources[0].url) == "https://reefs.example.org/a"
    assert sources[0].title.startswith("Reef report (attached web page")
    assert "bleach" in evidence[0].content


def test_ssrf_guard_blocks_private_targets_and_redirects() -> None:
    hop = tool(lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}))
    res = run(hop, "https://reefs.example.org/a")
    assert not res.success and "Could not read" in str(res.error)
    assert run(hop, "http://127.0.0.1/x").metadata["error_code"] == "blocked_url"


def test_offline_mode_explains_live_needed() -> None:
    res = run(WebPageReaderTool(offline=True, url_validator=public), "https://reefs.example.org/a")
    assert not res.success and res.error == OFFLINE_NOTE


def test_pages_split_long_text() -> None:
    pages = _pages("Sentence one. " * 800, size=1000)
    assert len(pages) > 5 and all(len(p["text"]) <= 1000 for p in pages)


def test_url_attachment_validation() -> None:
    assert ResearchAttachment(kind="url", data="https://x.org/a").kind == "url"
    for bad in ("javascript:alert(1)", "https://x.org/a b", "data:text/html;base64,AA"):
        with pytest.raises(ValueError):
            ResearchAttachment(kind="url", data=bad)
    with pytest.raises(ValueError):
        ResearchAttachment(kind="pdf", data="https://x.org/a.pdf")


def test_cli_turns_links_into_url_attachments() -> None:
    from deep_research.cli import attachment_for

    att = attachment_for("https://x.org/article")
    assert att.kind == "url" and att.data == "https://x.org/article"


def test_offline_run_with_url_attachment_explains_live_needed() -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings

    sub = ResearchSubmission(objective="When do coral reefs bleach?",
                             attachments=[ResearchAttachment(kind="url", data="https://example.org/reefs")])
    resp = asyncio.run(ResearchApplication(Settings(research_mode="offline")).submit(sub))
    assert resp.status == "failed" and resp.error == OFFLINE_NOTE
