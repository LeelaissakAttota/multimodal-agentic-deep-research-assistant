"""Dashboard logic kept free of Streamlit so it can be unit-tested.

* :class:`ControlClient` talks to the local control API (bearer token, 127.0.0.1).
* :class:`RunStore` persists finished/observed runs as JSON so "All Runs" survives restarts.
* Helpers turn API payloads into display rows (stages, citations, stats, provider health).

Nothing here ever returns or logs a full API key: key views come from the API's masked
``/control/keys`` endpoint only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8711"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
PDF_TYPES = {"pdf": "application/pdf"}
IMAGE_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
TERMINAL = ("completed", "failed", "stopped")

#: Engine step kind -> (stage label, agent name, one-line explanation).
STAGES: dict[str, tuple[str, str, str]] = {
    "planning": ("Plan", "planning_agent", "Splits the question into research tasks and picks tools."),
    "research_task": ("Research", "research_agent", "Runs one task: web search, page fetch, PDF or image read."),
    "analysis": ("Analyze", "analysis_agent", "Turns evidence into claims linked to sources."),
    "evaluation": ("Evaluate", "evaluation_agent", "Decides COMPLETE, CONTINUE or FAILED and lists gaps."),
    "reporting": ("Report", "report_agent", "Writes the Markdown report with numbered citations."),
}
STATE_ICON = {"succeeded": "✓", "approved": "✓", "running": "▶", "pending_approval": "!",
              "rejected": "✕", "failed": "✕", "skipped": "–"}


class ControlError(RuntimeError):
    """The control API is unreachable or returned an error (message is safe to show)."""


class ControlClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, *,
                 operator: str = "dashboard", timeout: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or os.environ.get("DEEP_RESEARCH_CONTROL_URL") or DEFAULT_API_URL).rstrip("/")
        self._token = token if token is not None else os.environ.get("DEEP_RESEARCH_CONTROL_TOKEN", "")
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport,
                                    headers={"X-Operator": operator})

    @property
    def has_token(self) -> bool:
        return len(self._token) >= 16

    def _call(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            resp = self._client.request(method, path, headers=headers, **kw)
        except httpx.HTTPError as exc:
            raise ControlError(f"control API not reachable at {self.base_url} ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ControlError(f"{resp.status_code}: {str(detail)[:200]}")
        return resp.json()

    def health(self) -> dict[str, Any]:
        return dict(self._call("GET", "/control/health"))

    def settings(self) -> dict[str, bool]:
        return dict(self._call("GET", "/control/settings"))

    def update_settings(self, **values: bool) -> dict[str, bool]:
        return dict(self._call("PUT", "/control/settings", json=values))

    def create_run(self, objective: str, *, mode: str, attachments: list[dict[str, str]] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"objective": objective, "mode": mode}
        if attachments:
            params["attachments"] = attachments
        return dict(self._call("POST", "/control/runs", json={"params": params}))

    def list_runs(self) -> list[dict[str, Any]]:
        return list(self._call("GET", "/control/runs"))

    def get_run(self, run_id: str) -> dict[str, Any]:
        return dict(self._call("GET", f"/control/runs/{run_id}"))

    def lifecycle(self, run_id: str, action: str) -> dict[str, Any]:
        if action not in ("pause", "resume", "stop"):
            raise ValueError(action)
        return dict(self._call("POST", f"/control/runs/{run_id}/{action}"))

    def decide(self, run_id: str, step_id: str, action: str, note: str | None = None) -> dict[str, Any]:
        if action not in ("approve", "reject"):
            raise ValueError(action)
        return dict(self._call("POST", f"/control/runs/{run_id}/steps/{step_id}/{action}", json={"note": note}))

    def keys(self) -> dict[str, Any]:
        return dict(self._call("GET", "/control/keys"))

    def set_key(self, name: str, value: str) -> dict[str, Any]:
        return dict(self._call("PUT", f"/control/keys/{name}", json={"value": value}))


# ----- attachments ---------------------------------------------------------------

def build_attachment(name: str, data: bytes) -> dict[str, str]:
    """Encode an uploaded PDF/image as the engine's ``data:`` URL attachment."""

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PDF_TYPES:
        kind, mime = "pdf", PDF_TYPES[ext]
    elif ext in IMAGE_TYPES:
        kind, mime = "image", IMAGE_TYPES[ext]
    else:
        raise ValueError("only PDF, PNG, JPG, WEBP or GIF files are supported")
    if not data:
        raise ValueError("the file is empty")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError("the file is larger than 20 MB")
    safe = re.sub(r"[^\w.\- ]", "_", Path(name).name)[:200]
    return {"kind": kind, "name": safe, "data": f"data:{mime};base64,{base64.b64encode(data).decode()}"}


# ----- persistence ---------------------------------------------------------------

class RunStore:
    """One JSON file per run under ``dashboard_runs/`` (attachment bytes are dropped)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _clean(run: dict[str, Any]) -> dict[str, Any]:
        data = json.loads(json.dumps(run, default=str))
        params = data.get("params") or {}
        if params.get("attachments"):
            params["attachments"] = [{"kind": a.get("kind"), "name": a.get("name")} for a in params["attachments"]]
        return dict(data)

    def save(self, run: dict[str, Any]) -> Path:
        run_id = str(run.get("run_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise ValueError("invalid run id")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{run_id}.json"
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._clean(run), fh, indent=1)
        os.replace(tmp, path)
        return path

    def get(self, run_id: str) -> dict[str, Any] | None:
        path = self.root / f"{run_id}.json"
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id) or not path.exists():
            return None
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            return None

    def list(self) -> list[dict[str, Any]]:
        out = []
        for path in self.root.glob("*.json") if self.root.exists() else []:
            try:
                out.append(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return sorted(out, key=lambda r: str(r.get("created_at", "")), reverse=True)


def merge_runs(live: list[dict[str, Any]], stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live API runs win over stored copies; stored-only runs keep showing after a restart."""

    seen = {r["run_id"]: r for r in stored if r.get("run_id")}
    seen.update({r["run_id"]: {**seen.get(r["run_id"], {}), **r} for r in live if r.get("run_id")})
    return sorted(seen.values(), key=lambda r: str(r.get("created_at", "")), reverse=True)


# ----- display helpers -------------------------------------------------------------

@dataclass(frozen=True)
class StageRow:
    index: int
    step_id: str
    label: str
    agent: str
    detail: str
    state: str
    risk: str
    decision: str

    @property
    def icon(self) -> str:
        return STATE_ICON.get(self.state, "…")

    @property
    def awaiting(self) -> bool:
        return self.state == "pending_approval"


def stage_rows(steps: list[dict[str, Any]]) -> list[StageRow]:
    rows: list[StageRow] = []
    for s in steps:
        label, agent, _ = STAGES.get(str(s.get("kind")), (str(s.get("kind", "step")).title(), str(s.get("kind")), ""))
        detail = str(s.get("name") or "")
        if s.get("error"):
            detail = f"{detail} · {s['error']}"
        rows.append(StageRow(int(s.get("index", len(rows))), str(s.get("step_id", "")), label, agent,
                             shorten(detail, 110), str(s.get("state", "")), str(s.get("risk", "")),
                             str(s.get("decision") or "")))
    return rows


def shorten(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def preview_text(markdown: str, limit: int = 600) -> str:
    """Plain-text preview of a Markdown report: skips headings and the attachment list."""

    keep = []
    for line in str(markdown).splitlines():
        t = line.strip()
        if not t or t.startswith("#") or t.lower().startswith("- attached"):
            continue
        keep.append(re.sub(r"[*_`>]+", "", t.lstrip("-• ")))
    return shorten(" ".join(keep), limit)


def report_of(run: dict[str, Any]) -> dict[str, Any] | None:
    result = run.get("result")
    if isinstance(result, dict) and isinstance(result.get("report"), dict):
        return dict(result["report"])
    return None


def citations(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in report.get("citations") or []:
        if isinstance(c, dict):
            out.append({"number": c.get("number"), "title": shorten(str(c.get("title") or "Untitled source"), 120),
                        "url": c.get("url"), "kind": c.get("kind") or ("web" if c.get("url") else "source"),
                        "page": c.get("page")})
    return out


def run_stats(run: dict[str, Any]) -> dict[str, str]:
    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    usage = ((result or {}).get("runtime") or {}).get("usage") or {}
    report = report_of(run) or {}
    calls = int(usage.get("tool_calls", 0)) + int(usage.get("model_calls", 0))
    elapsed = usage.get("elapsed_seconds")
    return {
        "status": str(run.get("status", "unknown")).replace("_", " ").title(),
        "steps": str(run.get("step_count", len(run.get("steps") or []))),
        "calls": str(calls) if usage else "–",
        "evidence": str(len(report.get("evidence_gathered") or [])) if report else "–",
        "elapsed": f"{float(elapsed):.1f} s" if isinstance(elapsed, (int, float)) else "–",
    }


def title_of(run: dict[str, Any]) -> str:
    return shorten(str((run.get("params") or {}).get("objective") or "(no question)"), 90)


def provider_health(settings: Any, masked_keys: dict[str, Any] | None, api_ok: bool) -> list[dict[str, str]]:
    """Rows for the Provider Health page. ``masked_keys`` comes from /control/keys (never raw values)."""

    keys = (masked_keys or {}).get("keys") or {}

    def key_state(name: str, env_present: bool) -> tuple[str, str]:
        info = keys.get(name) if isinstance(keys, dict) else None
        if isinstance(info, dict) and info.get("configured"):
            return "ok", f"set in key store ({info.get('masked', '****')})"
        if env_present:
            return "ok", "set via environment / .env"
        return "off", "not configured (offline fallback used)"

    def env(field: str) -> bool:
        return getattr(settings, field, None) is not None

    rows = [{"name": "Control API", "state": "ok" if api_ok else "down",
             "detail": "reachable on 127.0.0.1" if api_ok else "not reachable: start scripts/start-dashboard.ps1"}]
    for label, name, field in (("OpenRouter (free LLMs)", "OPENROUTER_API_KEY", "openrouter_api_key"),
                               ("Tavily search", "TAVILY_API_KEY", "tavily_api_key"),
                               ("NVIDIA speech-to-text", "NVIDIA_API_KEY", "nvidia_api_key"),
                               ("Hugging Face", "HUGGINGFACE_API_KEY", "huggingface_api_key")):
        state, detail = key_state(name, env(field))
        rows.append({"name": label, "state": state, "detail": detail})
    rows.append({"name": "DuckDuckGo / Wikipedia", "state": "ok", "detail": "keyless fallback search (live mode)"})
    rows.append({"name": "Free-model guard", "state": "ok", "detail": "only ':free' models are allowed"})
    return rows


def claims(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for c in report.get("claims") or []:
        if isinstance(c, dict) and c.get("text"):
            conf = c.get("confidence")
            out.append({"text": shorten(str(c["text"]), 240), "evidence": len(c.get("evidence_ids") or []),
                        "confidence": f"{float(conf):.0%}" if isinstance(conf, (int, float)) else "–"})
    return out


def local_time(value: Any) -> str:
    """ISO timestamp (UTC) -> local wall time with a zone label, e.g. '2026-10-24 22:18 IST'."""

    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
    except ValueError:
        return str(value or "")[:16]
    offset = dt.utcoffset()
    label = "IST" if offset is not None and offset.total_seconds() == 19800 else (dt.tzname() or "")
    return f"{dt:%Y-%m-%d %H:%M} {label}".strip()
