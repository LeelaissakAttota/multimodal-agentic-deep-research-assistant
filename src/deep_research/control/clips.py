"""Pages and selections clipped from the browser extension ("add as source")."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_CLIPS = 500


def clean_page_url(url: str) -> str:
    """http(s) only; drops credentials and the fragment. Raises ValueError for anything else."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or any(c.isspace() for c in url.strip()):
        raise ValueError("only http(s) page URLs can be clipped")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname  # IPv6 literal keeps its brackets
    host += f":{parts.port}" if parts.port else ""
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


class ClipStore:
    """Append-only JSONL (newest last), capped at :data:`MAX_CLIPS` entries."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def add(self, url: str, title: str = "", selection: str = "", *, by: str = "extension") -> dict[str, Any]:
        item = {"id": uuid.uuid4().hex[:12], "url": clean_page_url(url), "title": " ".join(title.split())[:300],
                "selection": selection.strip()[:5000], "by": by[:64],
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        with self._lock:
            items = [*self._read(), item][-MAX_CLIPS:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".part")
            tmp.write_text("".join(json.dumps(i, ensure_ascii=False) + "\n" for i in items), encoding="utf-8")
            os.replace(tmp, self.path)
        return item

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._read()))[:limit]
