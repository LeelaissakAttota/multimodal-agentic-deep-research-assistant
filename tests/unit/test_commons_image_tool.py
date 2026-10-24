"""Wikimedia Commons image search, network mocked."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from deep_research.domain.modality import Modality
from deep_research.tools.commons_image_tool import COMMONS_TOOL_ID, CommonsImageSearchTool
from deep_research.tools.tool import ToolRequest

PAGES = {"query": {"pages": [
    {"index": 2, "title": "File:Second.png", "imageinfo": [{"mime": "image/png",
     "descriptionurl": "https://commons.wikimedia.org/wiki/File:Second.png",
     "url": "https://upload.wikimedia.org/second.png", "extmetadata": {}}]},
    {"index": 1, "title": "File:Transformer diagram.svg", "imageinfo": [{
        "mime": "image/svg+xml", "descriptionurl": "https://commons.wikimedia.org/wiki/File:Transformer_diagram.svg",
        "url": "https://upload.wikimedia.org/t.svg", "thumburl": "https://upload.wikimedia.org/thumb/t.png",
        "width": 800, "height": 600,
        "extmetadata": {"ImageDescription": {"value": "<p>Diagram of the <b>Transformer</b></p>"},
                        "LicenseShortName": {"value": "CC BY-SA 4.0"}, "Artist": {"value": "<a>dvgodoy</a>"}}}]},
    {"index": 3, "title": "File:Clip.webm", "imageinfo": [{"mime": "video/webm",
     "descriptionurl": "https://commons.wikimedia.org/wiki/File:Clip.webm"}]},
]}}


def _client(calls: list[httpx.Request], status: int = 200, body: Any = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, json=PAGES if body is None else body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _run(tool: CommonsImageSearchTool, **params: Any) -> Any:
    return asyncio.run(tool.execute(ToolRequest(tool_name=COMMONS_TOOL_ID, parameters=params)))


def test_definition() -> None:
    d = CommonsImageSearchTool().get_definition()
    assert d.modality == Modality.IMAGE and d.cost_class == "FREE" and not d.auth_required


def test_search_orders_filters_and_attributes() -> None:
    calls: list[httpx.Request] = []
    result = _run(CommonsImageSearchTool(http_client=_client(calls)), query="transformer diagram")
    assert result.success
    rows = result.output.results
    assert [r["title"] for r in rows] == ["Transformer diagram.svg", "Second.png"]  # video dropped
    first = rows[0]
    assert first["url"] == "https://commons.wikimedia.org/wiki/File:Transformer_diagram.svg"
    assert first["image_url"] == "https://upload.wikimedia.org/thumb/t.png"
    assert first["license"] == "CC BY-SA 4.0" and first["artist"] == "dvgodoy"
    assert first["content"] == "Image: Diagram of the Transformer. Author: dvgodoy. Licence: CC BY-SA 4.0."
    assert rows[1]["image_url"] == "https://upload.wikimedia.org/second.png"
    assert calls[0].url.params["gsrnamespace"] == "6"


def test_empty_and_errors() -> None:
    assert _run(CommonsImageSearchTool(http_client=_client([], body={})), query="x").output.results == []
    assert _run(CommonsImageSearchTool(http_client=_client([])), query="").metadata["failure_kind"] == "invalid_input"
    failed = _run(CommonsImageSearchTool(http_client=_client([], status=503)), query="x")
    assert not failed.success and failed.metadata["failure_kind"] == "http_503"
