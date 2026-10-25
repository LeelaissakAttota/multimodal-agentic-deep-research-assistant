"""Self-contained static HTML export and revocable, token-protected read-only share links.

The HTML has inline CSS only (no scripts, fonts, images or other external assets) and every
report string is HTML-escaped. A share stores a snapshot of that HTML plus the SHA-256 hash
of a random token; the token itself is returned once and never stored or logged. Tokens are
checked with ``hmac.compare_digest``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from html import escape
from typing import Any

from deep_research.assistant.store import AssistantStore, now_iso

_SHARE_ID = re.compile(r"^[A-Za-z0-9_-]{8,32}$")
#: Response headers for the read-only share page (no scripts, no external loads, not indexed, not cached).
SHARE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "no-store",
}
_CSS = (
    "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:860px;margin:2rem auto;padding:0 1rem;"
    "color:#1d2433;line-height:1.55}h1{font-size:1.7rem}h2{margin-top:1.8rem;border-bottom:1px solid #e3e6ee}"
    "table{border-collapse:collapse;margin:1rem 0}td,th{border:1px solid #d5d9e3;padding:.3rem .6rem}"
    ".meta{color:#6b7385;font-size:.85rem}.cite{color:#3456d1;font-size:.8em}ol.src li{margin:.3rem 0}"
    ".claim{margin:.2rem 0}.badge{display:inline-block;min-width:3em;font-weight:600}"
)


def _cites(text: str) -> str:
    """Escape text, then mark ``[n]`` citations as links to the source list."""
    return re.sub(r"\[(\d{1,3})\]", r'<a class="cite" href="#src-\1">[\1]</a>', escape(text))


def _safe_url(url: Any) -> str | None:
    return url if isinstance(url, str) and re.match(r"^https?://[^\s\"'<>]+$", url) else None


def render_html(report: dict[str, Any]) -> str:
    """Report -> one self-contained HTML document (inline CSS, escaped content, no scripts)."""
    from deep_research.export import claim_line, parse, source_line

    doc = parse(report)
    body: list[str] = [f"<h1>{escape(doc.title)}</h1>"]
    body += [f'<p class="meta">{escape(m)}</p>' for m in doc.meta]
    body += _blocks_html(doc)
    if doc.claims:
        body.append("<h2>Claim confidence</h2>")
        body += [f'<p class="claim">{_cites(claim_line(c))}</p>' for c in doc.claims]
    if doc.sources:
        body.append('<h2>Sources</h2><ol class="src">')
        body += [_source_html(src, source_line(src)) for src in doc.sources]
        body.append("</ol>")
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<meta name=\"robots\" content=\"noindex,nofollow\">"
            f"<title>{escape(doc.title)}</title><style>{_CSS}</style></head><body>"
            + "\n".join(body) + "</body></html>\n")


def _block_html(block: Any) -> str:
    if block.kind in ("h1", "h2", "h3"):
        return f"<{block.kind}>{escape(block.text)}</{block.kind}>"
    if block.kind == "li":
        return f"<li>{_cites(block.text)}</li>"
    if block.kind == "table":
        rows = "".join("<tr>" + "".join(f"<td>{_cites(c)}</td>" for c in r) + "</tr>" for r in block.rows)
        return f"<table>{rows}</table>"
    return f"<p>{_cites(block.text)}</p>"


def _blocks_html(doc: Any) -> list[str]:
    out: list[str] = []
    in_list = False
    for block in doc.blocks:
        if (block.kind == "li") != in_list:
            out.append("</ul>" if in_list else "<ul>")
            in_list = not in_list
        if not (block.kind == "h1" and block.text == doc.title):
            out.append(_block_html(block))
    if in_list:
        out.append("</ul>")
    return out


def _source_html(src: dict[str, Any], line_text: str) -> str:
    num = escape(str(src.get("number", "")))
    url = _safe_url(src.get("url"))
    line = escape(line_text)
    link = f' <a href="{escape(url)}" rel="noopener noreferrer nofollow">open</a>' if url else ""
    return f'<li id="src-{num}" value="{num}">{line}{link}</li>' if num.isdigit() else f"<li>{line}{link}</li>"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ShareService:
    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    def create(self, run_id: str, report: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """Snapshot the report as HTML; returns (share info, token). The token is not stored."""
        if not isinstance(report, dict) or not report.get("content"):
            raise ValueError("report has no content to share")
        share_id = secrets.token_urlsafe(9)
        token = secrets.token_urlsafe(32)
        title = str(report.get("title") or "Research report")[:200]
        with self.store.connect() as db:
            db.execute("INSERT INTO shares (id, run_id, title, token_hash, html, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (share_id, run_id, title, _hash(token), render_html(report), now_iso()))
        return self.info(share_id), token  # type: ignore[return-value]

    def info(self, share_id: str) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute("SELECT id, run_id, title, created_at, revoked_at FROM shares WHERE id = ?",
                             (share_id,)).fetchone()
        return {**dict(row), "active": row["revoked_at"] is None} if row else None

    def list(self, run_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, run_id, title, created_at, revoked_at FROM shares"
        with self.store.connect() as db:
            rows = db.execute(sql + (" WHERE run_id = ?" if run_id else "") + " ORDER BY created_at DESC",
                              (run_id,) if run_id else ()).fetchall()
        return [{**dict(r), "active": r["revoked_at"] is None} for r in rows]

    def revoke(self, share_id: str) -> dict[str, Any]:
        with self.store.connect() as db:
            cur = db.execute("UPDATE shares SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (now_iso(), share_id))
            if cur.rowcount == 0:
                raise KeyError(share_id)
        return self.info(share_id)  # type: ignore[return-value]

    def open(self, share_id: str, token: str) -> str | None:
        """HTML for a valid, active share; ``None`` otherwise (unknown id, wrong token, revoked)."""
        if not _SHARE_ID.match(share_id or "") or not token or len(token) > 128:
            return None
        with self.store.connect() as db:
            row = db.execute("SELECT token_hash, html, revoked_at FROM shares WHERE id = ?", (share_id,)).fetchone()
        supplied = _hash(token)
        stored = row["token_hash"] if row else _hash("x" + supplied)  # same work for unknown ids
        if not hmac.compare_digest(supplied.encode(), str(stored).encode()) or row is None or row["revoked_at"]:
            return None
        return str(row["html"])
