"""``deep-research`` command line: run one research question end to end and optionally export it.

Examples::

    deep-research run "How do marine heatwaves affect coral reefs?"
    deep-research run "Summarise this paper" --file paper.pdf --mode live --export pdf
    deep-research run "Key points?" --file https://example.org/article --mode live --export docx --out out/

API keys come from the environment / ``.env`` exactly like the server; the CLI never prints them.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.config import Settings
from deep_research.domain.research_request import ResearchAttachment
from deep_research.tools.document_tool import DOCUMENT_SUFFIXES

MAX_FILE_BYTES = 20 * 1024 * 1024
_KIND_BY_SUFFIX = {".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image"}
_MEDIA_SUFFIXES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".ogg": "audio/ogg",
                   ".flac": "audio/flac", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
                   ".mkv": "video/x-matroska"}
_MEDIA_LINK = re.compile(r"^https?://([a-z0-9-]+\.)*(youtube\.com|youtu\.be|vimeo\.com)(/|$)", re.IGNORECASE)


def attachment_for(spec: str) -> ResearchAttachment:
    """Turn a ``--file`` argument (local path or http(s) URL) into a validated attachment."""

    if spec.lower().startswith(("http://", "https://")):
        link_kind = "media" if _MEDIA_LINK.match(spec) else "url"
        return ResearchAttachment.model_validate({"kind": link_kind, "data": spec, "name": spec[:200]})
    path = Path(spec).expanduser()
    if not path.is_file():
        raise SystemExit(f"--file not found: {spec}")
    suffix = path.suffix.lower()
    kind = _KIND_BY_SUFFIX.get(suffix) or ("document" if suffix in DOCUMENT_SUFFIXES else None) or (
        "media" if suffix in _MEDIA_SUFFIXES else None)
    if kind is None:
        raise SystemExit(f"unsupported --file type {path.suffix or '(none)'}; supported: {', '.join(supported_suffixes())}")
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        raise SystemExit(f"--file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB: {spec}")
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind == "pdf":
        mime = "application/pdf"
    elif kind == "document":
        mime = DOCUMENT_SUFFIXES[path.suffix.lower()]
    elif kind == "media":
        mime = _MEDIA_SUFFIXES[suffix]
    data = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return ResearchAttachment.model_validate({"kind": kind, "data": data, "name": path.name[:200]})


def supported_suffixes() -> list[str]:
    return [*_KIND_BY_SUFFIX, *DOCUMENT_SUFFIXES, *_MEDIA_SUFFIXES]


async def run_once(question: str, files: Sequence[str], *, mode: str | None, settings: Settings | None = None
                   ) -> dict[str, Any]:
    base = settings or Settings()
    if mode:
        base = base.model_copy(update={"research_mode": mode})
    app = ResearchApplication(base, max_sessions=1)
    submission = ResearchSubmission(objective=question, attachments=[attachment_for(f) for f in files])
    response = await app.submit(submission)
    data = response.model_dump(mode="json")
    data["mode"] = base.research_mode
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deep-research", description="Multimodal Agentic Deep Research Assistant")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="research one question and print the report")
    run.add_argument("question", help="the research question")
    run.add_argument("--file", action="append", default=[], metavar="PATH_OR_URL",
                     help="attach a PDF, image, DOCX, PPTX, TXT/MD file or a web page URL (repeatable, max 5)")
    run.add_argument("--mode", choices=("offline", "live"), help="offline (no network) or live (web + free models)")
    run.add_argument("--export", action="append", default=[], choices=("pdf", "docx", "pptx", "json", "md"),
                     help="also write the report in this format (repeatable)")
    run.add_argument("--out", default=".", help="directory for exported files (default: current directory)")
    run.add_argument("--json", action="store_true", help="print the full JSON response instead of the report text")
    bf = sub.add_parser("rag-backfill", help="chunk, embed and index saved reports for RAG follow-up chat")
    bf.add_argument("--runs-dir", default=None,
                    help="saved dashboard runs (default: $DEEP_RESEARCH_DASHBOARD_DIR or ./dashboard_runs)")
    bf.add_argument("--force", action="store_true", help="re-index reports that are already up to date")
    gs = sub.add_parser("graph-sync", help="MERGE every saved report's knowledge graph into Neo4j (MDRA_NEO4J_URI)")
    gs.add_argument("--runs-dir", default=None,
                    help="saved dashboard runs (default: $DEEP_RESEARCH_DASHBOARD_DIR or ./dashboard_runs)")
    return parser


def graph_sync(runs_dir: str | None) -> int:
    import os

    from deep_research.rag.backfill import saved_runs
    from deep_research.scale.graphrag import neo4j_configured, sync_runs

    if not neo4j_configured():
        print("[graph-sync: Neo4j is not configured (set MDRA_NEO4J_URI and install the 'graph' extra)]", file=sys.stderr)
        return 2
    folder = Path(runs_dir or os.environ.get("DEEP_RESEARCH_DASHBOARD_DIR") or Path.cwd() / "dashboard_runs").expanduser()
    runs = saved_runs(folder)
    try:
        res = sync_runs(runs)
    except Exception as exc:  # noqa: BLE001 - type only: never the URI or credentials
        print(f"[graph-sync failed: {type(exc).__name__}]", file=sys.stderr)
        return 1
    print(f"[graph-sync: {res['runs']} report(s) from {len(runs)} run(s) in {folder} | "
          f"{res['nodes']} nodes, {res['edges']} links merged]", file=sys.stderr)
    return 0


def rag_backfill(runs_dir: str | None, *, force: bool = False) -> int:
    import os

    from deep_research.rag.backfill import backfill, saved_runs
    from deep_research.rag.config import RagSettings
    from deep_research.rag.service import build_service, status

    settings = RagSettings()
    service = build_service(settings, require_enabled=False)
    if service is None:
        print(f"[rag-backfill: embeddings unavailable: {status(settings)['embeddings']}]", file=sys.stderr)
        return 2
    folder = Path(runs_dir or os.environ.get("DEEP_RESEARCH_DASHBOARD_DIR") or Path.cwd() / "dashboard_runs").expanduser()
    runs = saved_runs(folder)
    res = backfill(service, runs, force=force)
    print(f"[rag-backfill: {len(runs)} run(s) in {folder} | indexed {res.indexed} ({res.chunks} chunks) | "
          f"up to date {res.up_to_date} | no report {res.no_report} | failed {res.failed} | {service.label}]", file=sys.stderr)
    if service.reason and service.index.name != "pgvector":
        print(f"[index: local store ({service.reason})]", file=sys.stderr)
    for err in res.errors:
        print(f"[failed: {err}]", file=sys.stderr)
    if not settings.enabled:
        print("[note: MDRA_RAG_ENABLED is off, so chat still uses word overlap until you enable it]", file=sys.stderr)
    return 1 if res.failed else 0


def _safe_streams() -> None:
    """Never crash on non-UTF-8 consoles (Windows cp1252): replace unencodable characters."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _safe_streams()
    if args.command == "rag-backfill":
        return rag_backfill(args.runs_dir, force=args.force)
    if args.command == "graph-sync":
        return graph_sync(args.runs_dir)
    if len(args.file) > 5:
        raise SystemExit("at most 5 --file attachments")
    result = asyncio.run(run_once(args.question, args.file, mode=args.mode))
    report = result.get("report") if isinstance(result.get("report"), dict) else None
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    elif report:
        print(str(report.get("content") or "").strip())
    status = str(result.get("status"))
    print(f"\n[status: {status} | mode: {result.get('mode')}]", file=sys.stderr)
    if result.get("error"):
        print(f"[error: {result['error']}]", file=sys.stderr)
    if report and args.export:
        from deep_research.export import ExportUnavailable, export_filename, export_report

        out = Path(args.out).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        for fmt in dict.fromkeys(args.export):
            try:
                target = out / export_filename(report, fmt, str(result.get("session_id") or ""))
                target.write_bytes(export_report(report, fmt))
                print(f"[exported {fmt}: {target}]", file=sys.stderr)
            except ExportUnavailable as exc:
                print(f"[export {fmt} skipped: {exc}]", file=sys.stderr)
    return 0 if status == "completed" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
