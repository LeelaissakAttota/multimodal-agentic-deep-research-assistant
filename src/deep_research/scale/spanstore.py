"""Local span store for the dashboard's per-run trace timeline (``MDRA_TRACE_STORE=sqlite|jsonl``).

A :class:`LocalSpanProcessor` added to the tracer provider writes every finished span that belongs to a
research run (``mdra.run_id`` attribute, set from :func:`deep_research.scale.tracing.run_context`) to
``MDRA_TRACE_DIR`` (default ``data/traces``): one SQLite file ``spans.db`` or one ``<run_id>.jsonl`` per run.
Retention (``MDRA_TRACE_MAX_AGE_DAYS``, ``MDRA_TRACE_MAX_RUNS``) prunes whole runs at startup and then at most
every ``MDRA_TRACE_PRUNE_INTERVAL_SECONDS`` on write. Span names, attributes and error descriptions are
redacted again on write (:func:`deep_research.scale.tracing.safe_attributes`), so secrets
never reach the store even if a caller set raw attributes. Errors are stored as exception type only.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
RUN_ATTR = "mdra.run_id"
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")
_SCHEMA = ("CREATE TABLE IF NOT EXISTS spans (run_id TEXT NOT NULL, trace_id TEXT, span_id TEXT, parent_id TEXT, "
           "name TEXT, start_ns INTEGER, end_ns INTEGER, status TEXT, error TEXT, attrs TEXT, "
           "PRIMARY KEY (run_id, span_id))")


def safe_run_id(run_id: str) -> str:
    return _SAFE_ID.sub("_", str(run_id))[:128]


class SpanStore:
    def __init__(self, directory: str | Path, fmt: str = "sqlite", *, max_age_days: float = 0.0, max_runs: int = 0,
                 prune_interval: float = 3600.0) -> None:
        if fmt not in ("sqlite", "jsonl"):
            raise ValueError("trace store format must be sqlite or jsonl")
        self.dir = Path(directory)
        self.fmt = fmt
        self.max_age_days = max_age_days
        self.max_runs = max_runs
        self.prune_interval = prune_interval
        self._last_prune = 0.0
        self._lock = threading.Lock()

    def _run_ages(self) -> dict[str, float]:
        """Last activity (unix seconds) per stored run; keys are run ids (SQLite) or JSONL file stems."""
        if self.fmt == "sqlite":
            if not self.db_path.exists():
                return {}
            con = self._connect()
            try:
                return {str(r): float(e or 0) / 1e9 for r, e in
                        con.execute("SELECT run_id, MAX(end_ns) FROM spans GROUP BY run_id").fetchall()}
            finally:
                con.close()
        return {p.stem: p.stat().st_mtime for p in self.dir.glob("*.jsonl")} if self.dir.is_dir() else {}

    def prune(self, now: float | None = None) -> list[str]:
        """Delete runs older than ``max_age_days`` and beyond the newest ``max_runs``; returns removed run ids."""
        if not self.max_age_days and not self.max_runs:
            return []
        now = time.time() if now is None else now
        with self._lock:
            ages = self._run_ages()
            newest = sorted(ages, key=lambda r: (-ages[r], r))
            drop = set(newest[self.max_runs:]) if self.max_runs else set()
            if self.max_age_days:
                drop |= {r for r, t in ages.items() if now - t > self.max_age_days * 86400}
            if not drop:
                return []
            if self.fmt == "sqlite":
                con = self._connect()
                try:
                    with con:
                        con.executemany("DELETE FROM spans WHERE run_id = ?", [(r,) for r in sorted(drop)])
                finally:
                    con.close()
            else:
                for r in drop:
                    (self.dir / f"{r}.jsonl").unlink(missing_ok=True)
        return sorted(drop)

    def maybe_prune(self, *, force: bool = False) -> list[str]:
        """Prune when forced (startup) or when ``prune_interval`` has passed; never raises."""
        now = time.monotonic()
        if not force and (not self.prune_interval or now - self._last_prune < self.prune_interval):
            return []
        self._last_prune = now
        try:
            return self.prune()
        except (OSError, sqlite3.Error) as exc:
            log.warning("span store prune failed: %s", type(exc).__name__)
            return []

    @property
    def db_path(self) -> Path:
        return self.dir / "spans.db"

    def _connect(self) -> sqlite3.Connection:
        self.dir.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=10)
        con.execute(_SCHEMA)
        return con

    def write(self, rows: list[dict[str, Any]]) -> None:
        rows = [r for r in rows if r.get("run_id")]
        if not rows:
            return
        with self._lock:
            if self.fmt == "sqlite":
                con = self._connect()
                try:
                    with con:
                        con.executemany("INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?,?,?,?,?)", [
                            (r["run_id"], r["trace_id"], r["span_id"], r["parent_id"], r["name"], r["start_ns"],
                             r["end_ns"], r["status"], r["error"], json.dumps(r["attrs"], sort_keys=True)) for r in rows])
                finally:
                    con.close()
            else:
                self.dir.mkdir(parents=True, exist_ok=True)
                for r in rows:
                    with (self.dir / f"{safe_run_id(r['run_id'])}.jsonl").open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(r, sort_keys=True) + "\n")
        self.maybe_prune()  # periodic retention

    def load(self, run_id: str) -> list[dict[str, Any]]:
        """Spans of one run ordered by start time (empty when none were recorded)."""
        rows: list[dict[str, Any]] = []
        if self.fmt == "sqlite":
            if not self.db_path.exists():
                return []
            con = self._connect()
            try:
                cur = con.execute("SELECT run_id, trace_id, span_id, parent_id, name, start_ns, end_ns, status, error, "
                                  "attrs FROM spans WHERE run_id = ? ORDER BY start_ns", (str(run_id),))
                for rec in cur.fetchall():
                    keys = ("run_id", "trace_id", "span_id", "parent_id", "name", "start_ns", "end_ns", "status", "error")
                    row = dict(zip(keys, rec[:9]))
                    row["attrs"] = json.loads(rec[9] or "{}")
                    rows.append(row)
            finally:
                con.close()
            return rows
        path = self.dir / f"{safe_run_id(run_id)}.jsonl"
        if not path.exists():
            return []
        by_id: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("run_id") == str(run_id):
                by_id[str(row.get("span_id"))] = row
        return sorted(by_id.values(), key=lambda r: int(r.get("start_ns") or 0))


def span_row(span: Any) -> dict[str, Any] | None:
    """A redacted store row from a finished ``ReadableSpan`` (``None`` when it is not part of a run)."""
    from deep_research.scale.tracing import _clean, safe_attributes

    attrs = safe_attributes(dict(span.attributes or {}))
    run_id = attrs.get(RUN_ATTR)
    if not run_id:
        return None
    ctx = span.get_span_context()
    parent = getattr(span, "parent", None)
    status = getattr(span, "status", None)
    code = getattr(getattr(status, "status_code", None), "name", "UNSET")
    error = _clean(str(attrs.get("error.type") or (getattr(status, "description", None) or ""))) if code == "ERROR" else ""
    return {"run_id": str(run_id), "trace_id": f"{ctx.trace_id:032x}", "span_id": f"{ctx.span_id:016x}",
            "parent_id": f"{parent.span_id:016x}" if parent is not None else "", "name": _clean(str(span.name)),
            "start_ns": int(span.start_time or 0), "end_ns": int(span.end_time or span.start_time or 0),
            "status": code, "error": error[:200], "attrs": attrs}


def make_processor(store: SpanStore) -> Any:
    """An OpenTelemetry ``SpanProcessor`` that stamps the current run id on start and stores spans on end."""
    from opentelemetry.sdk.trace import SpanProcessor

    from deep_research.scale.tracing import current_run_id

    class LocalSpanProcessor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any = None) -> None:
            run_id = current_run_id()
            if run_id and not (span.attributes or {}).get(RUN_ATTR):
                span.set_attribute(RUN_ATTR, run_id)

        def on_end(self, span: Any) -> None:
            try:
                row = span_row(span)
                if row is not None:
                    store.write([row])
            except Exception as exc:  # noqa: BLE001 - tracing must never break a run
                log.warning("span store write failed: %s", type(exc).__name__)

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30000) -> bool:
            return True

    return LocalSpanProcessor()


def open_store() -> SpanStore | None:
    """The configured store, or ``None`` when ``MDRA_TRACE_STORE=off`` (default)."""
    from deep_research.scale.config import ScaleSettings

    s = ScaleSettings()
    return None if s.trace_store == "off" else SpanStore(s.trace_dir, s.trace_store)


def load_spans(run_id: str) -> list[dict[str, Any]]:
    store = open_store()
    if store is None:
        return []
    try:
        return store.load(run_id)
    except (OSError, sqlite3.Error, ValueError) as exc:
        log.warning("span store read failed: %s", type(exc).__name__)
        return []


def waterfall(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gantt rows: depth (from parent links), offset/width as % of the run, duration in ms, sorted by start."""
    if not spans:
        return []
    t0 = min(int(s["start_ns"]) for s in spans)
    t1 = max(int(s["end_ns"]) for s in spans)
    total = max(t1 - t0, 1)
    by_id = {s["span_id"]: s for s in spans}

    def depth(s: dict[str, Any]) -> int:
        d, cur, seen = 0, s, set()
        while cur.get("parent_id") in by_id and cur["span_id"] not in seen and d < 32:
            seen.add(cur["span_id"])
            cur, d = by_id[cur["parent_id"]], d + 1
        return d

    rows = []
    for s in sorted(spans, key=lambda x: (int(x["start_ns"]), x["span_id"])):
        start, end = int(s["start_ns"]), max(int(s["end_ns"]), int(s["start_ns"]))
        attrs = {k: v for k, v in (s.get("attrs") or {}).items() if k != RUN_ATTR}
        label = str(attrs.get("mdra.agent") or attrs.get("mdra.tool") or "")
        rows.append({"name": s["name"], "label": label, "depth": depth(s), "start_ms": (start - t0) / 1e6,
                     "duration_ms": (end - start) / 1e6, "offset_pct": 100 * (start - t0) / total,
                     "width_pct": max(100 * (end - start) / total, 0.4), "error": s.get("error") or "",
                     "status": s.get("status") or "UNSET", "attrs": attrs})
    return rows
