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


def attachment_for(spec: str) -> ResearchAttachment:
    """Turn a ``--file`` argument (local path or http(s) URL) into a validated attachment."""

    if spec.lower().startswith(("http://", "https://")):
        return ResearchAttachment.model_validate({"kind": "url", "data": spec, "name": spec[:200]})
    path = Path(spec).expanduser()
    if not path.is_file():
        raise SystemExit(f"--file not found: {spec}")
    kind = _KIND_BY_SUFFIX.get(path.suffix.lower()) or ("document" if path.suffix.lower() in DOCUMENT_SUFFIXES else None)
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
    data = f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"
    return ResearchAttachment.model_validate({"kind": kind, "data": data, "name": path.name[:200]})


def supported_suffixes() -> list[str]:
    return [*_KIND_BY_SUFFIX, *DOCUMENT_SUFFIXES]


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
    return parser


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
