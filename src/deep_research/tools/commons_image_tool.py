"""Keyless image search on Wikimedia Commons (freely licensed media).

Returns the file page (citable, carries licence/author) plus a thumbnail URL.
Hits use ``source_type="image"``; ``content`` is the caption/description so the
image can be cited for what it depicts, never for invented details.
"""

from __future__ import annotations

import time
from typing import Any

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
    hit,
    ok,
    read_query,
)
from deep_research.tools.tool import Tool, ToolRequest, ToolResult

COMMONS_TOOL_ID = "commons_image_search"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
_IMAGE_MIME = ("image/jpeg", "image/png", "image/gif", "image/webp", "image/svg+xml", "image/tiff")


class CommonsImageSearchTool(Tool):
    def __init__(self, *, http_client: httpx.AsyncClient | None = None, timeout: float = 15.0,
                 thumb_width: int = 640) -> None:
        self._client = http_client
        self._timeout = timeout
        self._width = thumb_width

    @property
    def name(self) -> str:
        return COMMONS_TOOL_ID

    def get_definition(self) -> ToolDefinition:
        return ToolDefinition(
            identifier=COMMONS_TOOL_ID, name="Image Search (Wikimedia Commons)",
            description="Find freely licensed images with captions and licence info (keyless).",
            modality=Modality.IMAGE, capabilities=[ToolCapability.SEARCH],
            input_schema=SourceSearchInput, output_schema=SourceSearchOutput,
            cost_class="FREE", network_required=True, auth_required=False, reliability="HIGH",
            metadata={"source_type": "image"},
        )

    async def health_check(self) -> bool:
        return True

    async def execute(self, request: ToolRequest) -> ToolResult:
        start = time.monotonic()
        try:
            query, limit = read_query(request.parameters)
        except ValueError as exc:
            return fail(COMMONS_TOOL_ID, start, str(exc), "invalid_input")
        params: dict[str, Any] = {
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrnamespace": 6, "gsrsearch": f"{query} filetype:bitmap|drawing", "gsrlimit": limit,
            "prop": "imageinfo", "iiprop": "url|mime|extmetadata", "iiurlwidth": self._width,
        }
        client = self._client or httpx.AsyncClient(follow_redirects=True)
        try:
            response = await client.get(COMMONS_API, params=params, headers={"User-Agent": USER_AGENT},
                                        timeout=self._timeout)
            response.raise_for_status()
            pages = (response.json().get("query") or {}).get("pages") or []
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return fail(COMMONS_TOOL_ID, start, "Wikimedia Commons search failed.", error_kind(exc))
        finally:
            if self._client is None:
                await client.aclose()
        ordered = sorted((p for p in pages if isinstance(p, dict)), key=lambda p: p.get("index", 0))
        rows = [r for r in (_row(p) for p in ordered) if r][:limit]
        return ok(COMMONS_TOOL_ID, start, rows, "wikimedia_commons")


def _meta(ext: dict[str, Any], key: str) -> str:
    value = ext.get(key)
    return clean(value.get("value") if isinstance(value, dict) else "", 500)


def _row(page: dict[str, Any]) -> dict[str, str] | None:
    infos = page.get("imageinfo") or []
    info = infos[0] if infos and isinstance(infos[0], dict) else {}
    if str(info.get("mime") or "") not in _IMAGE_MIME or not info.get("descriptionurl"):
        return None
    ext = info.get("extmetadata") or {}
    title = str(page.get("title") or "").removeprefix("File:")
    caption = _meta(ext, "ImageDescription") or _meta(ext, "ObjectName") or title.rsplit(".", 1)[0]
    licence = _meta(ext, "LicenseShortName")
    artist = _meta(ext, "Artist")
    content = f"Image: {caption}." + (f" Author: {artist}." if artist else "") + (f" Licence: {licence}." if licence else "")
    return hit(title=title, url=str(info["descriptionurl"]), snippet=caption, content=content,
               source_type="image", provider="wikimedia_commons",
               image_url=str(info.get("thumburl") or info.get("url") or ""),
               original_url=str(info.get("url") or ""), license=licence, artist=artist,
               width=str(info.get("width") or ""), height=str(info.get("height") or ""))
