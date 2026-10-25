"""Local span store + per-run trace timeline: off by default, run-id tagging, redaction, SQLite/JSONL, dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deep_research.scale import backend_status
from deep_research.scale.spanstore import SpanStore, load_spans, open_store, safe_run_id, waterfall
from deep_research.scale.tracing import (configure_tracing, current_run_id, reset_tracing, run_context, span,
                                         tracing_enabled)

ROOT = Path(__file__).resolve().parents[2]
SECRETS = ("sk-or-v1-abcdef1234567890", "hunter2pw", "abc.def.ghi")


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_tracing()
    yield
    reset_tracing()


def _row(rid: str, sid: str, parent: str, start: int, end: int, name: str = "mdra.agent", **kw: Any) -> dict[str, Any]:
    return {"run_id": rid, "trace_id": "t", "span_id": sid, "parent_id": parent, "name": name, "start_ns": start,
            "end_ns": end, "status": kw.get("status", "UNSET"), "error": kw.get("error", ""), "attrs": kw.get("attrs", {})}


def test_off_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert open_store() is None and load_spans("r1") == [] and not tracing_enabled()
    row = next(r for r in backend_status() if r["name"].startswith("Tracing"))
    assert row["state"] == "off"
    monkeypatch.setenv("MDRA_TRACE_STORE", "jsonl")
    row = next(r for r in backend_status() if r["name"].startswith("Tracing"))
    assert row["configured"] and "jsonl span store" in row["detail"]


@pytest.mark.parametrize("fmt", ["sqlite", "jsonl"])
def test_store_round_trip(tmp_path: Path, fmt: str) -> None:
    store = SpanStore(tmp_path, fmt)
    store.write([_row("r1", "b", "a", 5, 9), _row("r1", "a", "", 1, 10, "mdra.run"), _row("r2", "c", "", 0, 1),
                 _row("", "x", "", 0, 1)])
    store.write([_row("r1", "b", "a", 5, 8)])  # same span id rewritten -> one row
    rows = store.load("r1")
    assert [r["span_id"] for r in rows] == ["a", "b"] and rows[1]["end_ns"] == 8
    assert store.load("missing") == [] and [r["span_id"] for r in store.load("r2")] == ["c"]
    assert safe_run_id("../../etc/passwd") == ".._.._etc_passwd"
    with pytest.raises(ValueError):
        SpanStore(tmp_path, "csv")


def test_waterfall_rows() -> None:
    rows = waterfall([_row("r", "a", "", 0, 1_000_000_000, "mdra.run"),
                      _row("r", "b", "a", 250_000_000, 750_000_000, attrs={"mdra.agent": "research", "mdra.run_id": "r"}),
                      _row("r", "c", "b", 500_000_000, 500_000_000, "mdra.tool", status="ERROR", error="TimeoutError")])
    assert [r["depth"] for r in rows] == [0, 1, 2]
    assert rows[1]["offset_pct"] == 25 and rows[1]["width_pct"] == 50 and rows[1]["duration_ms"] == 500
    assert rows[1]["label"] == "research" and "mdra.run_id" not in rows[1]["attrs"]
    assert rows[2]["error"] == "TimeoutError" and rows[2]["width_pct"] == 0.4  # zero-length spans stay visible
    assert waterfall([]) == []


def test_spans_tagged_by_run_and_redacted(tmp_path: Path) -> None:
    store = SpanStore(tmp_path, "sqlite")
    exporter = InMemorySpanExporter()
    assert configure_tracing(exporter=exporter, store=store, force=True)
    with span("outside"):
        pass
    with run_context("run-1"):
        assert current_run_id() == "run-1"
        with span("mdra.run", api_key=SECRETS[0], note=f"Bearer {SECRETS[2]}", url=f"https://u:{SECRETS[1]}@h/x"):
            with pytest.raises(TimeoutError):
                with span("mdra.tool", **{"mdra.tool": "web"}):
                    raise TimeoutError(f"secret {SECRETS[1]}")

            async def child() -> None:
                with span("mdra.agent", **{"mdra.agent": "research"}):
                    await asyncio.sleep(0)

            asyncio.run(child())
    assert current_run_id() == ""
    rows = store.load("run-1")
    assert sorted(r["name"] for r in rows) == ["mdra.agent", "mdra.run", "mdra.tool"]
    tool = next(r for r in rows if r["name"] == "mdra.tool")
    assert tool["status"] == "ERROR" and tool["error"] == "TimeoutError"
    run = next(r for r in rows if r["name"] == "mdra.run")
    assert next(r for r in rows if r["name"] == "mdra.agent")["parent_id"] == run["span_id"]
    dump = (tmp_path / "spans.db").read_bytes()
    assert not any(s.encode() in dump for s in SECRETS) and b"api_key" not in dump
    assert len(exporter.get_finished_spans()) == 4  # the collector export still gets every span


def test_store_alone_enables_tracing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_TRACE_STORE", "jsonl")
    monkeypatch.setenv("MDRA_TRACE_DIR", str(tmp_path))
    assert tracing_enabled()
    with run_context("r9"), span("mdra.run"):
        pass
    assert [r["name"] for r in load_spans("r9")] == ["mdra.run"] and (tmp_path / "r9.jsonl").exists()


def test_full_offline_run_records_timeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_research.api.research_service import ResearchApplication, ResearchSubmission
    from deep_research.core.config import Settings

    monkeypatch.setenv("MDRA_TRACE_STORE", "sqlite")
    monkeypatch.setenv("MDRA_TRACE_DIR", str(tmp_path))
    settings = Settings(_env_file=None, research_mode="offline")
    response = asyncio.run(ResearchApplication(settings).submit(
        ResearchSubmission(objective="What are the health benefits of green tea?"), run_id="ctl-1"))
    assert response.status == "completed"
    rows = waterfall(load_spans("ctl-1"))
    names = {r["name"] for r in rows}
    assert {"mdra.run", "mdra.agent", "mdra.tool"} <= names and rows[0]["name"] == "mdra.run" and rows[0]["depth"] == 0
    assert {"planning", "reporting"} <= {r["label"] for r in rows}


def test_dashboard_trace_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    from tests.unit.test_dashboard_assistant import APP, _setup

    _setup(tmp_path, monkeypatch)
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params.update({"page": "researches", "run": "tea1", "view": "report"})
    at.run()
    assert any("Trace timeline is off" in c.value for c in at.caption)
    monkeypatch.setenv("MDRA_TRACE_STORE", "sqlite")
    monkeypatch.setenv("MDRA_TRACE_DIR", str(tmp_path / "traces"))
    SpanStore(tmp_path / "traces").write([
        _row("tea1", "a", "", 0, 2_000_000_000, "mdra.run", attrs={"mdra.mode": "offline"}),
        _row("tea1", "b", "a", 0, 1_000_000_000, "mdra.tool", status="ERROR", error="TimeoutError",
             attrs={"mdra.tool": "web"})])
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params.update({"page": "researches", "run": "tea1", "view": "report"})
    at.run()
    assert not at.exception
    text = " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)
    assert "2 spans" in text and "1 with errors" in text and "mdra.tool · web" in text and "TimeoutError" in text
