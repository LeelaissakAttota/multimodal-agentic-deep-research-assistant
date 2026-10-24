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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from deep_research.evidence.scoring import claim_scores, clean_claim_text, credibility

DEFAULT_API_URL = "http://127.0.0.1:8711"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
PDF_TYPES = {"pdf": "application/pdf"}
DOC_TYPES = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             "txt": "text/plain", "md": "text/markdown", "markdown": "text/markdown"}
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


def display_zone() -> Any:
    """Zone for all displayed times: DEEP_RESEARCH_DISPLAY_TZ, default Asia/Kolkata (IST)."""

    try:
        return ZoneInfo(os.environ.get("DEEP_RESEARCH_DISPLAY_TZ") or "Asia/Kolkata")
    except (ZoneInfoNotFoundError, ValueError):
        return None


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

    def logs(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return list(self._call("GET", f"/control/runs/{run_id}/logs", params={"after": after}).get("logs") or [])

    def keys(self) -> dict[str, Any]:
        return dict(self._call("GET", "/control/keys"))

    def set_key(self, name: str, value: str) -> dict[str, Any]:
        return dict(self._call("PUT", f"/control/keys/{name}", json={"value": value}))


# ----- attachments ---------------------------------------------------------------

def build_attachment(name: str, data: bytes) -> dict[str, str]:
    """Encode an uploaded PDF/image/document as the engine's ``data:`` URL attachment."""

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PDF_TYPES:
        kind, mime = "pdf", PDF_TYPES[ext]
    elif ext in IMAGE_TYPES:
        kind, mime = "image", IMAGE_TYPES[ext]
    elif ext in DOC_TYPES:
        kind, mime = "document", DOC_TYPES[ext]
    else:
        raise ValueError("only PDF, DOCX, PPTX, TXT, MD, PNG, JPG, WEBP or GIF files are supported")
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
                        "page": c.get("page"),
                        "credibility": c.get("credibility") or credibility(c.get("url"), kind=c.get("kind"))})
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
    """Per-claim confidence rows: scored cited statements when the report has citations, else engine claims."""

    scored = report.get("claim_scores")
    if scored is None and report.get("citations"):
        scored = claim_scores(str(report.get("content") or ""),
                              [{**c, "credibility": c["credibility"]} for c in citations(report)])
    if scored:
        return [{"text": shorten(clean_claim_text(str(c.get("text", ""))), 240),
                 "evidence": len(c.get("citations") or []), "citations": list(c.get("citations") or []),
                 "confidence": f"{float(c.get('confidence', 0)):.0%}", "score": float(c.get("confidence", 0)),
                 "label": str(c.get("label") or ""), "reason": str(c.get("reason") or ""),
                 "section": str(c.get("section") or "")}
                for c in scored if isinstance(c, dict) and c.get("text")]
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
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(display_zone())
    except ValueError:
        return str(value or "")[:16]
    offset = dt.utcoffset()
    label = "IST" if offset is not None and offset.total_seconds() == 19800 else (dt.tzname() or "")
    return f"{dt:%Y-%m-%d %H:%M} {label}".strip()


# ----- v2 dashboard aggregates (all derived from real runs, never invented) -------------------

APP_TITLE = "Multimodal Agentic Deep Research Assistant"
REPO_URL = "https://github.com/LeelaissakAttota/multimodal-agentic-deep-research-assistant"
ACTIVE = ("queued", "running", "paused", "waiting_approval", "stopping")


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def duration_seconds(run: dict[str, Any]) -> float | None:
    """Wall-clock seconds of a finished run: engine usage first, else finished_at - created_at."""

    result = run.get("result") if isinstance(run.get("result"), dict) else {}
    elapsed = (((result or {}).get("runtime") or {}).get("usage") or {}).get("elapsed_seconds")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        return float(elapsed)
    start, end = _parse(run.get("created_at")), _parse(run.get("finished_at"))
    if start and end and end >= start:
        return (end - start).total_seconds()
    return None


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "–"
    if seconds < 1:
        return "< 1 s"
    if seconds < 60:
        return f"{seconds:.0f} s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


def summary(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts for the four dashboard tiles."""

    total = len(runs)
    done = [r for r in runs if r.get("status") == "completed"]
    active = [r for r in runs if r.get("status") not in TERMINAL]
    failed = [r for r in runs if r.get("status") in ("failed", "stopped")]
    durations = [d for d in (duration_seconds(r) for r in done) if d is not None]
    avg = sum(durations) / len(durations) if durations else None
    return {"total": total, "completed": len(done), "active": len(active), "failed": len(failed),
            "success_rate": f"{len(done) / total:.0%}" if total else "–",
            "avg_seconds": avg, "avg": fmt_duration(avg), "timed": len(durations),
            "awaiting": sum(bool(r.get("pending_approvals")) for r in active)}


def status_badge(status: Any) -> tuple[str, str]:
    """(label, css class) for a run status."""

    s = str(status or "unknown")
    if s == "completed":
        return "Completed", "b-ok"
    if s in ("failed", "stopped"):
        return s.title(), "b-bad"
    if s == "waiting_approval":
        return "Needs approval", "b-wait"
    return "In Progress" if s in ACTIVE else s.replace("_", " ").title(), "b-run"


def run_tags(run: dict[str, Any]) -> list[str]:
    """Factual tags: mode, attachment kinds, approval mode, citation count."""

    params = run.get("params") or {}
    tags = [str(params.get("mode") or "offline").title()]
    kinds = {str(a.get("kind")) for a in params.get("attachments") or [] if isinstance(a, dict)}
    tags += [label for kind, label in (("pdf", "PDF"), ("document", "Doc"), ("image", "Image"), ("url", "Link"))
             if kind in kinds]
    if run.get("approval_mode"):
        tags.append("Step approval")
    report = report_of(run)
    if report and report.get("citations"):
        tags.append(f"{len(report['citations'])} sources")
    return tags


def filter_runs(runs: list[dict[str, Any]], query: str = "", status: str = "All") -> list[dict[str, Any]]:
    q = query.strip().lower()
    out = []
    for r in runs:
        label = status_badge(r.get("status"))[0]
        if status != "All" and label != status:
            continue
        hay = " ".join([title_of(r), str(r.get("run_id", "")), " ".join(run_tags(r))]).lower()
        if q and q not in hay:
            continue
        out.append(r)
    return out


def _domain(url: Any) -> str:
    m = re.match(r"https?://(?:www\.)?([^/:?#]+)", str(url or ""))
    return m.group(1) if m else ""


def source_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every cited source across saved reports, de-duplicated by URL (or title for non-web)."""

    seen: dict[str, dict[str, Any]] = {}
    for r in runs:
        report = report_of(r)
        if not report:
            continue
        for c in citations(report):
            key = str(c.get("url") or f"{c['kind']}:{c['title']}")
            row = seen.setdefault(key, {**c, "domain": _domain(c.get("url")) or c["kind"], "runs": [],
                                        "first_seen": r.get("created_at")})
            if r.get("run_id") not in row["runs"]:
                row["runs"].append(r.get("run_id"))
    return sorted(seen.values(), key=lambda x: (-len(x["runs"]), str(x["title"])))


def source_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "")
    if kind in ("pdf", "document"):
        return "Documents"
    if kind == "image":
        return "Images"
    return "Web" if row.get("url") else "Other"


def knowledge(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Knowledge-base counts and items: reports, claims, sources and uploaded files."""

    reports, claim_rows, files = [], [], []
    for r in runs:
        params = r.get("params") or {}
        for a in params.get("attachments") or []:
            if isinstance(a, dict):
                files.append({"name": str(a.get("name") or "file"), "kind": str(a.get("kind") or ""),
                              "run_id": r.get("run_id"), "when": r.get("created_at")})
        report = report_of(r)
        if report:
            reports.append({"title": title_of(r), "run_id": r.get("run_id"), "when": r.get("created_at"),
                            "claims": len(claims(report)), "citations": len(citations(report))})
            claim_rows += [{**c, "run_id": r.get("run_id")} for c in claims(report)]
    sources = source_rows(runs)
    return {"reports": reports, "claims": claim_rows, "sources": sources, "files": files,
            "web": sum(source_kind(s) == "Web" for s in sources),
            "documents": sum(f["kind"] in ("pdf", "document") for f in files),
            "images": sum(f["kind"] == "image" for f in files)}


def _ocr_ready() -> bool:
    import importlib.util

    return any(importlib.util.find_spec(m) is not None for m in ("rapidocr_onnxruntime", "pytesseract"))


def _export_ready() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("fpdf", "docx", "pptx"))


def tool_catalog(settings: Any, masked_keys: dict[str, Any] | None, mode: str) -> list[dict[str, str]]:
    """Real engine tools with an honest state: on / needs key / offline only / coming soon."""

    keys = (masked_keys or {}).get("keys") or {}

    def has(name: str, field: str) -> bool:
        info = keys.get(name) if isinstance(keys, dict) else None
        return bool(isinstance(info, dict) and info.get("configured")) or getattr(settings, field, None) is not None

    live = mode == "live"
    tavily = has("TAVILY_API_KEY", "tavily_api_key")
    rows = [
        {"name": "Web Search", "detail": "Tavily, then DuckDuckGo / Wikipedia (keyless)" if tavily
         else "DuckDuckGo / Wikipedia, keyless (Tavily key not set)",
         "state": "on" if live else "live mode", "icon": "🌐"},
        {"name": "Web Page Fetch", "detail": "reads up to the configured number of public pages per task",
         "state": "on" if live else "live mode", "icon": "🔗"},
        {"name": "Web Page Reader", "detail": "reads a page you link in From URL as a cited source (SSRF-guarded)",
         "state": "on" if live else "live mode", "icon": "🧭"},
        {"name": "Document Analysis", "detail": "PDF, DOCX, PPTX, TXT and Markdown with page/slide citations", "state": "on", "icon": "📄"},
        {"name": "Image Analysis", "detail": "free vision model (OpenRouter key) + local OCR (RapidOCR), metadata fallback",
         "state": "on" if has("OPENROUTER_API_KEY", "openrouter_api_key") else "needs key", "icon": "🖼️"},
        {"name": "LLM Agents", "detail": "planning, analysis, evaluation and reporting on ':free' models",
         "state": "on" if has("OPENROUTER_API_KEY", "openrouter_api_key") else "needs key", "icon": "🧠"},
        {"name": "OCR", "detail": "local RapidOCR for images and scanned PDFs (free, no key)" if _ocr_ready()
         else "pip install rapidocr-onnxruntime to read text in images and scans",
         "state": "on" if _ocr_ready() else "not installed", "icon": "🔤"},
        {"name": "Source Credibility", "detail": "domain heuristic on every cited source + per-claim confidence",
         "state": "on", "icon": "🛡️"},
        {"name": "Report Export", "detail": "PDF, DOCX, PPTX, JSON and Markdown downloads" if _export_ready()
         else "JSON/Markdown; pip install fpdf2 python-docx python-pptx for PDF/DOCX/PPTX",
         "state": "on", "icon": "📤"},
        {"name": "Audio Analysis", "detail": "NVIDIA speech-to-text (engine tool, not exposed in the dashboard yet)",
         "state": "coming soon", "icon": "🎙️"},
        {"name": "Video Analysis", "detail": "not supported by the engine yet", "state": "coming soon", "icon": "🎬"},
    ]
    return rows


class Prefs:
    """Small JSON preferences file next to the saved runs (no secrets)."""

    DEFAULTS: dict[str, Any] = {"default_mode": "offline", "step_approval": True, "list_limit": 5,
                                "show_closed": True}

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return {k: data.get(k, v) for k, v in self.DEFAULTS.items()}

    def save(self, **values: Any) -> dict[str, Any]:
        data = {**self.load(), **{k: v for k, v in values.items() if k in self.DEFAULTS}}
        if data["default_mode"] not in ("offline", "live"):
            raise ValueError("default_mode must be offline or live")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        return data


def needs_detail(run: dict[str, Any]) -> bool:
    """A finished run listed without its result should be fetched once (to get report + citations)."""

    return run.get("status") == "completed" and "result" not in run


MAX_URL_BYTES = 20 * 1024 * 1024
_PAGE_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


def attachment_from_url(url: str, *, transport: httpx.BaseTransport | None = None,  # noqa: C901 - one guarded download
                        validator: Any = None) -> dict[str, str]:
    """Attach a public link: PDF/image/Office files are downloaded; web pages become ``url`` attachments.

    Every hop goes through the engine's SSRF guard (public http(s) hosts only, redirects re-checked),
    and downloads are capped at 20 MB. Web pages are read by the engine in live mode.
    """

    from deep_research.tools.web_tools import is_public_http_url

    check = validator or is_public_http_url
    url = url.strip()
    if not re.match(r"https?://[^\s/]+\.[^\s]+$", url):
        raise ValueError("enter a full https:// link to a web page, PDF, document or image")
    files = {v: k for k, v in {**PDF_TYPES, **IMAGE_TYPES, **DOC_TYPES}.items()}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False, transport=transport) as http:
            for _ in range(6):
                if not check(url):
                    raise ValueError("that link points to a private or non-public address")
                with http.stream("GET", url) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        url = str(resp.url.join(resp.headers["location"]))
                        continue
                    if resp.status_code >= 400:
                        raise ValueError(f"the link returned HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype in _PAGE_TYPES or not ctype:
                        return {"kind": "url", "name": shorten(url, 200), "data": url}
                    ext = files.get(ctype)
                    if ext is None:
                        raise ValueError(f"unsupported link type {ctype}; use a web page, PDF, DOCX, PPTX, TXT/MD or image")
                    body = b""
                    for chunk in resp.iter_bytes():
                        body += chunk
                        if len(body) > MAX_URL_BYTES:
                            raise ValueError("the linked file is larger than 20 MB")
                    break
            else:
                raise ValueError("too many redirects")
    except httpx.HTTPError as exc:
        raise ValueError(f"could not download the link ({type(exc).__name__})") from None
    name = Path(url.split("?")[0]).name or "download"
    if not name.lower().endswith(f".{ext}") and not (ext == "jpeg" and name.lower().endswith(".jpg")):
        name = f"{name}.{ext}"
    return build_attachment(name, body)


def md_safe(markdown: str) -> str:
    """Escape ``$`` so Streamlit does not render prices like "$4,000–7,000" as LaTeX."""

    return re.sub(r"(?<!\\)\$", r"\\$", str(markdown))


def live_feed(logs: list[dict[str, Any]], *, max_cards: int = 8, max_lines: int = 60) -> dict[str, Any]:
    """Split the run log into source cards (real URLs/files the agents read) and terminal lines."""

    cards: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    for item in logs:
        seq = int(item.get("seq", len(lines)))
        kind = str(item.get("event") or "")
        if kind == "source":
            cards.append({"seq": seq, "title": shorten(str(item.get("title") or item.get("url") or "source"), 90),
                          "url": str(item.get("url") or ""), "domain": _domain(item.get("url")) or "attached file",
                          "snippet": shorten(str(item.get("snippet") or ""), 160)})
        tag = {"plan": "PLAN", "search": "SEARCH", "source": "FETCH", "verdict": "EVAL"}.get(kind)
        if not tag:
            msg = str(item.get("message", ""))
            tag = "ERROR" if item.get("level") == "error" else "WAIT" if "awaiting approval" in msg else "STEP"
        dt = _parse(item.get("at"))
        when = dt.astimezone(display_zone()).strftime("%H:%M:%S") if dt else ""
        lines.append({"seq": seq, "time": when, "tag": tag, "text": shorten(str(item.get("message", "")), 220)})
    queries = sum(line["tag"] == "SEARCH" for line in lines)
    return {"cards": cards[-max_cards:][::-1], "lines": lines[-max_lines:], "sources": len(cards), "queries": queries}
