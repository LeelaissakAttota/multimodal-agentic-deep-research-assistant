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
from urllib.parse import urlparse
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
MEDIA_TYPES = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac",
               "mp4": "video/mp4", "webm": "video/webm", "mov": "video/quicktime", "mkv": "video/x-matroska"}
_MEDIA_HOSTS = ("youtube.com", "youtu.be", "vimeo.com")
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

    def create_run(self, objective: str, *, mode: str, attachments: list[dict[str, str]] | None = None,
                   approval_mode: bool | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"objective": objective, "mode": mode}
        if attachments:
            params["attachments"] = attachments
        body: dict[str, Any] = {"params": params}
        if approval_mode is not None:
            body["approval_mode"] = bool(approval_mode)
        return dict(self._call("POST", "/control/runs", json=body))

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

    def create_share(self, run_id: str, report: dict[str, Any] | None = None,
                     expires_in_days: float | None = None) -> dict[str, Any]:
        """New read-only share link; ``url`` holds the token and is shown once (it is not stored)."""
        body: dict[str, Any] = {"report": report}
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        share = dict(self._call("POST", f"/control/runs/{run_id}/shares", json=body))
        share["url"] = self.base_url + str(share.pop("path", ""))
        return share

    def list_shares(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._call("GET", "/control/shares", params={"run_id": run_id} if run_id else None))

    def revoke_share(self, share_id: str) -> dict[str, Any]:
        return dict(self._call("POST", f"/control/shares/{share_id}/revoke"))

    def save_artifact(self, run_id: str, fmt: str, report: dict[str, Any] | None = None) -> dict[str, Any]:
        """Store an export in the artifact store (local folder, or S3/MinIO when MDRA_S3_* is set)."""
        return dict(self._call("POST", f"/control/runs/{run_id}/artifacts", params={"format": fmt}, json={"report": report}))

    def list_artifacts(self, run_id: str | None = None) -> list[dict[str, Any]]:
        return list(self._call("GET", "/control/artifacts", params={"run_id": run_id} if run_id else None))

    def clips(self, limit: int = 50) -> list[dict[str, Any]]:
        """Pages sent from the browser extension as sources (newest first)."""
        return list(self._call("GET", "/control/clips", params={"limit": limit}))


# ----- attachments ---------------------------------------------------------------

def build_attachment(name: str, data: bytes) -> dict[str, str]:
    """Encode an uploaded PDF/image/document/audio/video file as the engine's ``data:`` URL attachment."""

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in PDF_TYPES:
        kind, mime = "pdf", PDF_TYPES[ext]
    elif ext in IMAGE_TYPES:
        kind, mime = "image", IMAGE_TYPES[ext]
    elif ext in DOC_TYPES:
        kind, mime = "document", DOC_TYPES[ext]
    elif ext in MEDIA_TYPES:
        kind, mime = "media", MEDIA_TYPES[ext]
    else:
        raise ValueError("only PDF, DOCX, PPTX, TXT, MD, PNG, JPG, WEBP, GIF or audio/video "
                         "(MP3, WAV, M4A, OGG, FLAC, MP4, WEBM, MOV, MKV) files are supported")
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


#: Engine fallback claims that only name an evidence id (deterministic analysis); not worth showing.
_PLACEHOLDER_CLAIM = re.compile(r"^Claim based on evidence [0-9a-f-]{8,}$", re.I)


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
        if isinstance(c, dict) and c.get("text") and not _PLACEHOLDER_CLAIM.match(str(c["text"])):
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
            verdicts = row.setdefault("verdicts", {})
            for v in _verdicts_for_citation(report, c.get("number")):
                verdicts[v] = verdicts.get(v, 0) + 1
    return sorted(seen.values(), key=lambda x: (-len(x["runs"]), str(x["title"])))


#: Citation ``kind`` of public-source tools -> Sources-page filter label.
SOURCE_KIND_LABELS = {"academic": "Academic", "video": "Video", "social": "Discussions"}
SOURCE_FILTERS = ["All Sources", "Web", "Academic", "Video", "Discussions", "Images", "Documents"]
KIND_ICONS = {"Web": ("🌐", "c1"), "Academic": ("🎓", "c2"), "Video": ("🎬", "c6"), "Discussions": ("💬", "c5"),
              "Images": ("🖼️", "c4"), "Documents": ("📄", "c3"), "Other": ("📎", "c3")}


def _verdicts_for_citation(report: dict[str, Any], number: Any) -> list[str]:
    result = report.get("verification")
    if not isinstance(result, dict) or number is None:
        return []
    return [str(r.get("verdict")) for r in result.get("claims") or []
            if isinstance(r, dict) and number in (r.get("citations") or [])]


_SHORT = (("supported", "✓"), ("partially_supported", "~"), ("unsupported", "?"), ("contradicted", "✗"))


def verdict_label(counts: dict[str, Any] | None) -> str:
    """Compact verdict counts, e.g. '3✓ 1~ 1✗'; '' when nothing was checked."""
    counts = counts or {}
    return " ".join(f"{int(counts[k])}{sym}" for k, sym in _SHORT if counts.get(k))


def verification_summary(report: dict[str, Any]) -> dict[str, Any] | None:
    """Support score and verdict counts of one report, or None when it was not fact-checked."""
    result = report.get("verification")
    if not isinstance(result, dict) or not result.get("total"):
        return None
    counts = {k: int(v) for k, v in (result.get("counts") or {}).items() if isinstance(v, (int, float))}
    score = result.get("support_score")
    return {"score": float(score) if isinstance(score, (int, float)) else None, "counts": counts,
            "label": verdict_label(counts), "contradictions": len(report.get("contradictions") or [])}


def source_kind(row: dict[str, Any]) -> str:
    kind = str(row.get("kind") or "")
    if kind in ("pdf", "document"):
        return "Documents"
    if kind == "image":
        return "Images"
    if kind in SOURCE_KIND_LABELS:
        return SOURCE_KIND_LABELS[kind]
    return "Web" if row.get("url") else "Other"


def _source_tool_state(settings: Any, mode: str, tool_id: str) -> str:
    try:
        enabled = settings.enabled_source_tools()
    except AttributeError:
        enabled = []
    if tool_id not in enabled:
        return "disabled"
    return "on" if mode == "live" else "live mode"


#: (tool id, dashboard name, detail, icon, filter label) for the keyless public-source tools.
SOURCE_TOOL_ROWS = (
    ("academic_search", "Academic Search", "arXiv, Semantic Scholar, OpenAlex (keyless)", "🎓", "Academic"),
    ("youtube_transcript", "YouTube Transcripts", "public caption tracks of videos (keyless)", "🎬", "Video"),
    ("reddit_search", "Reddit Discussions", "Reddit posts + comments (official OAuth if keys set, else public JSON)", "💬", "Discussions"),
    ("commons_image_search", "Image Search", "Wikimedia Commons, freely licensed, with licence/author", "🖼️", "Images"),
    ("hackernews_search", "Hacker News", "HN stories + comments via the Algolia HN API (keyless)", "🟧", "Discussions"),
    ("bluesky_search", "Bluesky Posts", "public post search (keyless AppView; optional BSKY_HANDLE/APP_PASSWORD)", "🦋", "Discussions"),
    ("stackexchange_search", "Stack Exchange", "Stack Overflow + stats/math/physics/quant/money Q&A (keyless)", "🧮", "Discussions"),
)


def source_catalog(settings: Any, mode: str) -> list[dict[str, str]]:
    """Source types for the Sources page with their real state (no 'coming soon' placeholders)."""
    live = mode == "live"
    rows = [
        {"name": "Web Search", "detail": "Tavily / DuckDuckGo / Wikipedia", "icon": "🌐", "cls": "c1",
         "state": "Active" if live else "Live mode"},
        {"name": "Documents", "detail": "PDF, DOCX, PPTX, TXT/MD", "icon": "📄", "cls": "c3", "state": "Active"},
        {"name": "Uploaded images", "detail": "Local image upload", "icon": "🖼️", "cls": "c4", "state": "Active"},
        {"name": "Web links", "detail": "Web pages, PDF / doc / image URLs", "icon": "🔗", "cls": "c5", "state": "Active"},
    ]
    for tool_id, name, detail, icon, label in SOURCE_TOOL_ROWS:
        state = _source_tool_state(settings, mode, tool_id)
        rows.append({"name": name, "detail": detail, "icon": icon, "cls": KIND_ICONS[label][1], "tool": tool_id,
                     "state": {"on": "Active", "live mode": "Live mode", "disabled": "Disabled"}[state]})
    return rows


def knowledge(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Knowledge-base counts and items: reports, claims, sources and uploaded files."""

    reports, claim_rows, files = [], [], []
    verified: list[dict[str, Any]] = []
    for r in runs:
        params = r.get("params") or {}
        for a in params.get("attachments") or []:
            if isinstance(a, dict):
                files.append({"name": str(a.get("name") or "file"), "kind": str(a.get("kind") or ""),
                              "run_id": r.get("run_id"), "when": r.get("created_at")})
        report = report_of(r)
        if report:
            ver = verification_summary(report)
            reports.append({"title": title_of(r), "run_id": r.get("run_id"), "when": r.get("created_at"),
                            "claims": len(claims(report)), "citations": len(citations(report)), "verification": ver})
            if ver:
                verified.append(ver)
            claim_rows += [{**c, "run_id": r.get("run_id")} for c in claims(report)]
    sources = source_rows(runs)
    return {"reports": reports, "claims": claim_rows, "sources": sources, "files": files,
            "web": sum(source_kind(s) == "Web" for s in sources),
            "documents": sum(f["kind"] in ("pdf", "document") for f in files),
            "images": sum(f["kind"] == "image" for f in files),
            "videos": sum(f["kind"] == "media" for f in files), "verification": _aggregate(verified)}



def graph_inputs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-report claims + citations for :func:`deep_research.scale.graph.build_graph`."""
    out = []
    for r in runs:
        report = report_of(r)
        if report and report.get("claim_scores"):
            # raw claim text: the same node ids the report chat's graph retrieval looks up
            from deep_research.scale.graphrag import report_input

            out.append(report_input(str(r.get("run_id") or ""), report, title_of(r)))
        elif report:
            out.append({"run_id": r.get("run_id"), "title": title_of(r), "claims": claims(report),
                        "citations": citations(report), "contradictions": report.get("contradictions") or []})
    return out


def knowledge_graph(runs: list[dict[str, Any]]) -> Any:
    from deep_research.scale.graph import build_graph

    return build_graph(graph_inputs(runs))


def _aggregate(verified: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for v in verified:
        for k, n in v["counts"].items():
            counts[k] = counts.get(k, 0) + n
    scores = [v["score"] for v in verified if v["score"] is not None]
    return {"reports": len(verified), "counts": counts, "label": verdict_label(counts),
            "score": sum(scores) / len(scores) if scores else None,
            "contradictions": sum(v["contradictions"] for v in verified)}


def verification_line(agg: dict[str, Any]) -> str:
    """One-line fact-check status for the Sources / Knowledge Base pages."""
    if not agg.get("reports"):
        return "No fact-checked reports yet. Live runs verify every cited claim."
    score = f"{agg['score']:.0%}" if agg.get("score") is not None else "–"
    return (f"{agg['reports']} fact-checked report(s) · mean support {score} · {agg['label'] or 'no verdicts'}"
            f" · {agg['contradictions']} contradiction(s)")


def _ocr_ready() -> bool:
    import importlib.util

    return any(importlib.util.find_spec(m) is not None for m in ("rapidocr_onnxruntime", "pytesseract"))


def _export_ready() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("fpdf", "docx", "pptx"))


def memory_status(vector_dir: Path | None = None) -> dict[str, Any]:
    """Which long-term memory components are active (no model is loaded to answer this)."""

    import importlib.util

    from deep_research.memory.vector_store import DEFAULT_VECTOR_DIR

    vdir = vector_dir or DEFAULT_VECTOR_DIR
    has_st = importlib.util.find_spec("sentence_transformers") is not None
    has_chroma = importlib.util.find_spec("chromadb") is not None
    on_disk = sum(f.stat().st_size for f in vdir.rglob("*") if f.is_file()) if vdir.exists() else 0
    return {"embedder": "all-MiniLM-L6-v2 (local)" if has_st else "hashing (deterministic fallback)",
            "reranker": "ms-marco-MiniLM-L-6-v2 (local)" if has_st else "lexical overlap (fallback)",
            "vector_store": "Chroma (local)" if has_chroma else "local JSON (fallback)",
            "path": str(vdir), "bytes_on_disk": on_disk}


def memory_items(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Claims and cited sources from saved reports, as indexable memory items."""

    items: dict[str, dict[str, Any]] = {}
    for r in runs:
        report = report_of(r)
        if not report:
            continue
        rid = str(r.get("run_id"))
        for i, c in enumerate(claims(report)):
            items[f"{rid}:claim:{i}"] = {"kind": "claim", "text": c["text"], "run_id": rid, "title": title_of(r)}
        for c in citations(report):
            key = f"source:{c.get('url') or c['title']}"
            items.setdefault(key, {"kind": "source", "text": str(c["title"]), "run_id": rid, "title": title_of(r),
                                   "url": c.get("url")})
    return [{"id": k, **v} for k, v in items.items()]


def persistent_memory_search(runs: list[dict[str, Any]], query: str, k: int, vector_dir: Path) -> list[dict[str, Any]]:
    """Search the on-disk long-term vector store written by research runs; [] when absent/unusable."""

    if not vector_dir.is_dir() or not any(vector_dir.iterdir()):
        return []
    try:
        from deep_research.memory.embeddings import get_embedder
        from deep_research.memory.reranker import LexicalReranker
        from deep_research.memory.vector_store import get_vector_store

        store = get_vector_store(vector_dir)
        if store.count() == 0:
            return []
        hits = [h for h in store.search(get_embedder().embed([query])[0], k * 3) if h.score > 0]
    except Exception:  # noqa: BLE001 - corrupt store / dimension mismatch -> fall back
        return []
    by_session = {str((r.get("result") or {}).get("session_id")): r for r in runs if isinstance(r.get("result"), dict)}
    rr = LexicalReranker().score(query, [h.text for h in hits])
    ranked = sorted(zip(hits, rr), key=lambda p: (-(0.5 * p[0].score + 0.5 * p[1]), p[0].id))
    out = []
    for h, r in ranked[:k]:
        run = by_session.get(str(h.metadata.get("session_id")))
        out.append({"id": h.id, "kind": "memory", "text": shorten(h.text, 240), "score": round(0.5 * h.score + 0.5 * r, 3),
                    "run_id": run.get("run_id") if run else None,
                    "title": title_of(run) if run else f"session {str(h.metadata.get('session_id'))[:8]}",
                    "backend": "persistent"})
    return out


def rag_line(service: Any) -> str:
    """Knowledge Base status row for report RAG (no model is loaded to answer this)."""
    if service is not None:
        return str(service.label)
    from deep_research.rag.service import status

    st = status()
    if not st["enabled"]:
        return "off (MDRA_RAG_ENABLED=false) · word overlap"
    return f"word overlap fallback · {st['embeddings']}"


def rag_search(service: Any, runs: list[dict[str, Any]], query: str, k: int = 10) -> list[dict[str, Any]]:
    """Hybrid BM25 + vector search over every saved report's chunks; [] when RAG is off or fails.

    Reports not yet indexed (or changed since) are indexed first; up-to-date ones cost a hash check.
    """
    if service is None or not query.strip():
        return []
    by_id: dict[str, dict[str, Any]] = {}
    try:
        for r in runs:
            report = report_of(r)
            if report and r.get("run_id"):
                by_id[str(r["run_id"])] = r
                service.index_report(str(r["run_id"]), report, title=title_of(r))
        hits = service.search(query, k)
    except Exception:  # noqa: BLE001 - fall back to the memory search
        return []
    out = []
    for h in hits:
        run = by_id.get(h.chunk.run_id)
        cites = "".join(f"[{n}]" for n in h.chunk.citations)
        out.append({"id": h.chunk.chunk_id, "kind": "claim" if h.chunk.kind == "claim" else "source" if h.chunk.kind == "source"
                    else "memory", "text": shorten(f"{h.chunk.text} {cites}".strip(), 240), "score": round(h.score, 4),
                    "run_id": h.chunk.run_id if run else None, "title": title_of(run) if run else h.chunk.title or h.chunk.run_id,
                    "citations": list(h.chunk.citations), "backend": service.label})
    return out


def memory_search(runs: list[dict[str, Any]], query: str, k: int = 10,
                  vector_dir: Path | None = None) -> list[dict[str, Any]]:
    """Semantic search: the persistent vector store when present, else an in-memory index of saved runs."""

    if vector_dir is not None and query.strip():
        persistent = persistent_memory_search(runs, query, k, vector_dir)
        if persistent:
            return persistent

    from deep_research.memory.embeddings import HashingEmbedder
    from deep_research.memory.reranker import LexicalReranker
    from deep_research.memory.vector_store import LocalVectorStore

    items = memory_items(runs)
    if not query.strip() or not items:
        return []
    emb, store = HashingEmbedder(), LocalVectorStore()
    store.upsert([i["id"] for i in items], emb.embed([i["text"] for i in items]), [i["text"] for i in items],
                 [{"n": n} for n in range(len(items))])
    hits = [h for h in store.search(emb.embed([query])[0], k * 3) if h.score > 0]
    rr = LexicalReranker().score(query, [h.text for h in hits])
    ranked = sorted(zip(hits, rr), key=lambda p: (-(0.5 * p[0].score + 0.5 * p[1]), p[0].id))
    by_id = {i["id"]: i for i in items}
    return [{**by_id[h.id], "score": round(0.5 * h.score + 0.5 * r, 3), "backend": "in-memory"} for h, r in ranked[:k]]


def tool_catalog(settings: Any, masked_keys: dict[str, Any] | None, mode: str) -> list[dict[str, str]]:
    """Real engine tools with an honest state: on / needs key / offline only / coming soon."""

    keys = (masked_keys or {}).get("keys") or {}
    media = media_status()

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
        *({"name": name, "detail": detail, "state": _source_tool_state(settings, mode, tool_id), "icon": icon,
           "tool": tool_id} for tool_id, name, detail, icon, _ in SOURCE_TOOL_ROWS),
        {"name": "OCR", "detail": "local RapidOCR for images and scanned PDFs (free, no key)" if _ocr_ready()
         else "pip install rapidocr-onnxruntime to read text in images and scans",
         "state": "on" if _ocr_ready() else "not installed", "icon": "🔤"},
        {"name": "Source Credibility", "detail": "domain heuristic on every cited source + per-claim confidence",
         "state": "on", "icon": "🛡️"},
        {"name": "Report Export", "detail": "PDF, DOCX, PPTX, JSON and Markdown downloads" if _export_ready()
         else "JSON/Markdown; pip install fpdf2 python-docx python-pptx for PDF/DOCX/PPTX",
         "state": "on", "icon": "📤"},
        {"name": "Audio Analysis", "detail": f"timestamped transcripts of audio uploads: {media['line']}",
         "state": media["state"], "icon": "🎙️"},
        {"name": "Video Analysis", "detail": f"video files and YouTube links (transcript + keyframes): {media['line']}",
         "state": media["state"], "icon": "🎬"},
    ]
    return rows


class Prefs:
    """Small JSON preferences file next to the saved runs (no secrets)."""

    DEFAULTS: dict[str, Any] = {"default_mode": "offline", "step_approval": False, "list_limit": 5,
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
    if is_media_link(url):
        if not check(url):
            raise ValueError("that link points to a private or non-public address")
        return {"kind": "media", "name": shorten(url, 200), "data": url}
    files = {v: k for k, v in {**PDF_TYPES, **IMAGE_TYPES, **DOC_TYPES, **MEDIA_TYPES}.items()}
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
                        raise ValueError(f"unsupported link type {ctype}; use a web page, PDF, DOCX, PPTX, TXT/MD, "
                                         "image, audio/video file or YouTube link")
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


def page_label(page: Any) -> str:
    """`` p.3`` for documents, `` at 01:23`` for audio/video timestamps, empty when unknown."""

    if not page:
        return ""
    return f" at {page}" if ":" in str(page) else f" p.{page}"


def is_media_link(url: str) -> bool:
    """YouTube/Vimeo pages are analysed as media (yt-dlp in the engine), not downloaded here."""

    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _MEDIA_HOSTS)


def media_status() -> dict[str, Any]:
    """Real state of the optional media stack (no import of heavy modules)."""

    import importlib.util
    import shutil

    from deep_research.media.config import MediaSettings

    try:
        settings = MediaSettings()
        enabled, transcriber = settings.enabled, settings.transcriber
    except Exception:  # noqa: BLE001 - a bad env value must not break the dashboard
        enabled, transcriber = False, "local"
    found = {name: importlib.util.find_spec(mod) is not None
             for name, mod in (("faster-whisper", "faster_whisper"), ("yt-dlp", "yt_dlp"), ("PyAV", "av"))}
    found["ffmpeg"] = shutil.which("ffmpeg") is not None
    missing = [n for n, ok in found.items() if not ok and not (n == "faster-whisper" and transcriber == "api")
               and n != "ffmpeg" and not (n == "PyAV" and found["ffmpeg"])]
    if not enabled:
        state, line = "off", "off - set MDRA_MEDIA_ENABLED=true and pip install -e \".[media]\""
    elif missing:
        state, line = "not installed", f"missing {', '.join(missing)} - pip install -e \".[media]\""
    else:
        frames = "ffmpeg" if found["ffmpeg"] else "PyAV"
        engine = "faster-whisper (local)" if transcriber == "local" else "local/free transcription API"
        state, line = "on", f"{engine}; YouTube captions via yt-dlp; keyframes via {frames} + vision model"
    return {"enabled": enabled, "state": state, "line": line, "found": found, "ready": state == "on"}


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


def report_charts(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Chart specs saved with a report -> ``{"title", "unit", "labels", "values"}`` rows for native charts."""
    out = []
    for chart in report.get("charts") or []:
        if not isinstance(chart, dict):
            continue
        data = [d for d in chart.get("data") or [] if isinstance(d, dict) and isinstance(d.get("value"), (int, float))]
        if data:
            out.append({"id": str(chart.get("id") or ""), "title": str(chart.get("title") or "Chart"),
                        "unit": str(chart.get("unit") or ""), "labels": [str(d.get("label")) for d in data],
                        "values": [float(d["value"]) for d in data]})
    return out


_VERDICT_BADGE = {"supported": ("Supported", "b-ok"), "partially_supported": ("Partial", "b-wait"),
                  "unsupported": ("Unsupported", "b-off"), "contradicted": ("Contradicted", "b-bad"),
                  "no_evidence": ("No evidence", "b-off")}


def verification_rows(report: dict[str, Any]) -> dict[str, Any]:
    """Per-claim verdicts for the report viewer: score, method and rows with a badge label/class."""
    result = report.get("verification")
    if not isinstance(result, dict):
        return {"score": None, "methods": [], "rows": [], "contradictions": []}
    rows = []
    for r in result.get("claims") or []:
        if isinstance(r, dict):
            label, css = _VERDICT_BADGE.get(str(r.get("verdict")), (str(r.get("verdict")), "b-off"))
            rows.append({"claim": shorten(str(r.get("claim", "")), 220), "label": label, "css": css,
                         "cites": ", ".join(str(n) for n in r.get("citations") or []),
                         "method": "LLM judge" if str(r.get("method", "")).startswith("llm") else "offline heuristic"})
    conflicts = [c for c in report.get("contradictions") or [] if isinstance(c, dict)]
    score = result.get("support_score")
    return {"score": f"{float(score):.0%}" if isinstance(score, (int, float)) else None,
            "methods": list(result.get("methods") or []), "rows": rows, "contradictions": conflicts}


def strip_section(markdown: str, title: str) -> str:
    """Drop one ``## title`` section (used to show native charts instead of the text charts)."""
    import re

    return re.sub(rf"(?ms)^## {re.escape(title)}\s*\n.*?(?=^## |\Z)", "", markdown).rstrip() + "\n"


STEP_LABEL = "Step-by-step approval"
STEP_HELP = "Asks for your OK before each important step"


def approval_state_html(run: dict[str, Any]) -> str:
    """Run page line: is Step-by-step approval on for this run, and where the value came from."""
    on = bool(run.get("approval_mode"))
    src = {"run": "set for this run", "settings": "from Settings"}.get(str(run.get("approval_source") or ""), "")
    text = (f"<b>{STEP_LABEL}: {'ON' if on else 'OFF'}</b>" + (f" <span class='muted'>({src})</span>" if src else "")
            + (" — each stage waits for your Approve or Reject." if on else " — stages run straight through."))
    return f"<div class='note'>{text}</div>"
