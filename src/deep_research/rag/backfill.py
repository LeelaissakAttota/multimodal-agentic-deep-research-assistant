"""Index reports that were saved before RAG was enabled (``deep-research rag-backfill``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deep_research.rag.service import RagService


@dataclass
class BackfillResult:
    indexed: int = 0
    up_to_date: int = 0
    no_report: int = 0
    failed: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)


def saved_runs(runs_dir: Path) -> list[dict[str, Any]]:
    """Runs saved by the dashboard (``dashboard_runs/*.json``); unreadable files are skipped."""
    out = []
    for path in sorted(runs_dir.glob("*.json")) if runs_dir.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("run_id"):
            out.append(data)
    return out


def run_report(run: dict[str, Any]) -> dict[str, Any] | None:
    result = run.get("result")
    if isinstance(result, dict) and isinstance(result.get("report"), dict):
        return dict(result["report"])
    return None


def run_title(run: dict[str, Any]) -> str:
    params = run.get("params") if isinstance(run.get("params"), dict) else {}
    return str((params or {}).get("objective") or run.get("title") or run.get("run_id") or "")[:200]


def backfill(service: RagService, runs: list[dict[str, Any]], *, force: bool = False) -> BackfillResult:
    res = BackfillResult()
    for run in runs:
        report = run_report(run)
        if report is None:
            res.no_report += 1
            continue
        rid = str(run["run_id"])
        try:
            n = service.index_report(rid, report, title=run_title(run), force=force)
        except Exception as exc:  # noqa: BLE001 - keep going; report the type only
            res.failed += 1
            res.errors.append(f"{rid}: {type(exc).__name__}")
            continue
        if n:
            res.indexed += 1
            res.chunks += n
        else:
            res.up_to_date += 1
    return res
