"""Dashboard helpers: control client, run store, attachments, display rows, start script."""

from __future__ import annotations

import base64
import json
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl, _attachments
from deep_research.core.config import Settings
from deep_research.dashboard import core
from deep_research.dashboard.core import (
    ControlClient, ControlError, RunStore, build_attachment, citations, claims, local_time, merge_runs,
    preview_text, provider_health, run_stats, shorten, stage_rows,
)

TOKEN = "d" * 32
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def api(tmp_path: Path) -> ControlClient:
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path)
    app = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="deep-research")
    test_client = TestClient(app)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = test_client.request(request.method, request.url.path, params=request.url.params,
                                   headers=dict(request.headers), content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    return ControlClient("http://test", TOKEN, transport=httpx.MockTransport(handler))


def _wait(api: ControlClient, run_id: str, pred: Any, timeout: float = 15.0) -> dict[str, Any]:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = api.get_run(run_id)
        if pred(run):
            return run
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_client_full_supervised_flow_with_pdf(api: ControlClient) -> None:
    pdf = (ROOT / "examples/demo-data/data/field_note.pdf").read_bytes()
    api.update_settings(approval_mode=True, auto_approve_low_risk=False)
    run = api.create_run("What are SMR benefits?", mode="offline", attachments=[build_attachment("field_note.pdf", pdf)])
    rid = run["run_id"]
    for _ in range(30):
        cur = _wait(api, rid, lambda r: r["pending_approvals"] or r["status"] in core.TERMINAL)
        if cur["status"] in core.TERMINAL:
            break
        api.decide(rid, cur["pending_approvals"][0], "approve")
    assert cur["status"] == "completed"
    rows = stage_rows(cur["steps"])
    assert rows[0].label == "Plan" and rows[-1].label == "Report"
    assert all(r.state == "succeeded" for r in rows)
    assert "field_note.pdf" in json.dumps(cur["result"]["report"].get("attachments"))


def test_client_reject_stops_research(api: ControlClient) -> None:
    api.update_settings(approval_mode=True)
    rid = api.create_run("Reject me", mode="offline")["run_id"]
    cur = _wait(api, rid, lambda r: r["pending_approvals"])
    api.decide(rid, cur["pending_approvals"][0], "reject")
    done = _wait(api, rid, lambda r: r["status"] in core.TERMINAL)
    assert done["steps"][0]["state"] == "rejected"


def test_client_errors_are_safe() -> None:
    bad = ControlClient("http://127.0.0.1:9", TOKEN, timeout=0.5)
    with pytest.raises(ControlError, match="not reachable"):
        bad.health()


def test_wrong_token_and_keys_masked(api: ControlClient, tmp_path: Path) -> None:
    api.set_key("OPENROUTER_API_KEY", "sk-or-v1-abcdefghijklmnop1234")
    shown = json.dumps(api.keys())
    assert "abcdefghijklmnop" not in shown and "1234" in shown
    wrong = ControlClient("http://test", "x" * 32, transport=api._client._transport)
    with pytest.raises(ControlError, match="401"):
        wrong.settings()
    with pytest.raises(ValueError):
        api.decide("r", "s", "delete")


def test_build_attachment_kinds_and_limits() -> None:
    a = build_attachment("../evil/report.PDF", b"%PDF-1.4")
    assert a["kind"] == "pdf" and a["name"] == "report.PDF"
    assert base64.b64decode(a["data"].split(",", 1)[1]) == b"%PDF-1.4"
    assert build_attachment("chart.jpg", b"x")["data"].startswith("data:image/jpeg;base64,")
    for name, data in (("a.exe", b"x"), ("a.png", b""), ("a.pdf", b"x" * (core.MAX_ATTACHMENT_BYTES + 1))):
        with pytest.raises(ValueError):
            build_attachment(name, data)


def test_service_attachment_validation() -> None:
    assert _attachments(None) == []
    with pytest.raises(ValueError):
        _attachments("nope")
    with pytest.raises(ValueError):
        _attachments([{"kind": "exe", "data": "x"}])


def test_run_store_persists_and_drops_attachment_bytes(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    run = {"run_id": "abc123", "status": "completed", "created_at": "2026-10-24T10:00:00+00:00",
           "params": {"objective": "q", "attachments": [{"kind": "pdf", "name": "f.pdf", "data": "data:xx"}]}}
    store.save(run)
    store.save({"run_id": "zzz", "created_at": "2026-10-24T11:00:00+00:00"})
    again = RunStore(tmp_path / "runs")
    assert [r["run_id"] for r in again.list()] == ["zzz", "abc123"]
    assert "data" not in json.dumps(again.get("abc123")["params"]["attachments"])
    assert again.get("../etc") is None
    with pytest.raises(ValueError):
        store.save({"run_id": "../x"})


def test_merge_runs_prefers_live() -> None:
    stored = [{"run_id": "a", "status": "running", "created_at": "1", "result": {"x": 1}},
              {"run_id": "b", "status": "completed", "created_at": "0"}]
    live = [{"run_id": "a", "status": "completed", "created_at": "1"}]
    merged = merge_runs(live, stored)
    assert merged[0]["status"] == "completed" and merged[0]["result"] == {"x": 1}
    assert {r["run_id"] for r in merged} == {"a", "b"}


def test_display_helpers() -> None:
    assert shorten("a " * 200, 20).endswith("…") and len(shorten("a " * 200, 20)) <= 20
    rows = stage_rows([{"index": 0, "step_id": "s", "kind": "planning", "name": "planning (iteration 0)",
                        "state": "pending_approval", "risk": "low"},
                       {"index": 1, "kind": "mystery", "state": "failed", "error": "boom"}])
    assert rows[0].awaiting and rows[0].icon == "!" and rows[0].agent == "planning_agent"
    assert rows[1].label == "Mystery" and "boom" in rows[1].detail
    report = {"content": "## Head\n- Attached pdf: x\nBody **bold** [1]", "citations": [
        {"number": 1, "title": "T", "url": None, "kind": "attachment", "page": 2}, "junk"],
        "claims": [{"text": "C", "evidence_ids": ["e"], "confidence": 0.8}], "evidence_gathered": ["e"]}
    assert preview_text(report["content"]) == "Body bold [1]"
    assert citations(report) == [{"number": 1, "title": "T", "url": None, "kind": "attachment", "page": 2}]
    assert claims(report) == [{"text": "C", "evidence": 1, "confidence": "80%"}]
    run = {"status": "waiting_approval", "step_count": 3,
           "result": {"report": report, "runtime": {"usage": {"tool_calls": 2, "model_calls": 1, "elapsed_seconds": 4.25}}}}
    assert run_stats(run) == {"status": "Waiting Approval", "steps": "3", "calls": "3", "evidence": "1", "elapsed": "4.2 s"}
    assert run_stats({})["calls"] == "–"
    assert local_time("bad")[:3] == "bad"
    assert local_time("2026-10-24T10:00:00+00:00").startswith("2026-10-24")


def test_provider_health_never_shows_raw_keys() -> None:
    masked = {"keys": {"OPENROUTER_API_KEY": {"configured": True, "masked": "****wxyz"}}}
    rows = provider_health(Settings(research_mode="offline"), masked, api_ok=False)
    by = {r["name"]: r for r in rows}
    assert by["Control API"]["state"] == "down"
    assert by["OpenRouter (free LLMs)"]["state"] == "ok" and "****wxyz" in by["OpenRouter (free LLMs)"]["detail"]
    assert by["Tavily search"]["state"] == "off"


def test_start_script_and_app_present() -> None:
    script = (ROOT / "scripts/start-dashboard.ps1").read_text(encoding="utf-8")
    assert "8502" in script and "8711" in script and "127.0.0.1" in script
    assert "Write-Host $env:DEEP_RESEARCH_CONTROL_TOKEN" not in script
    app = (ROOT / "src/deep_research/dashboard/app.py").read_text(encoding="utf-8")
    for page in ("New Research", "Live Run", "Research Report", "My Researches", "Provider Health", "Settings"):
        assert page in app
    assert "dashboard_runs/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


def test_app_smoke_renders_every_page(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    RunStore(tmp_path).save(_run("abc123", cites=2, atts=["pdf"]))
    views = [{"page": p} for p in ("dashboard", "new", "researches", "knowledge", "sources", "tools", "settings", "bogus")]
    views += [{"page": "researches", "run": "abc123", "view": "report"}, {"page": "new", "run": "abc123", "view": "live"},
              {"page": "researches", "run": "missing", "view": "report"}]
    for q in views:
        at = testing.AppTest.from_file(str(ROOT / "src/deep_research/dashboard/app.py"), default_timeout=30)
        for k, v in q.items():
            at.query_params[k] = v
        at.run()
        assert not at.exception, q
        text = " ".join(m.value for m in at.markdown)
        assert "Multimodal Agentic Deep Research Assistant" in text and "Control API offline" in text


# ----- v2 aggregates -------------------------------------------------------------------------------

def _run(rid: str, status: str = "completed", *, cites: int = 0, mode: str = "offline",
         atts: list[str] | None = None, elapsed: float | None = 30.0) -> dict[str, Any]:
    report = {"content": "# R\nPrice is $4,000.", "claims": [{"text": "c", "confidence": 0.8, "evidence_ids": ["e"]}],
              "citations": [{"number": i + 1, "title": f"S{i}", "url": f"https://www.site{i}.org/p"} for i in range(cites)]}
    run: dict[str, Any] = {"run_id": rid, "status": status, "created_at": f"2026-10-24T10:0{len(rid) % 9}:00+00:00",
                           "params": {"objective": f"Q {rid}", "mode": mode,
                                      "attachments": [{"kind": k, "name": f"f.{k}"} for k in atts or []]}}
    if status == "completed":
        run["result"] = {"report": report, "runtime": {"usage": {"elapsed_seconds": elapsed}}}
    return run


def test_summary_counts_only_real_runs() -> None:
    assert core.summary([])["total"] == 0 and core.summary([])["avg"] == "–"
    runs = [_run("a", elapsed=30), _run("b", elapsed=90), _run("c", "failed"),
            {**_run("d", "waiting_approval"), "pending_approvals": ["s1"]}]
    s = core.summary(runs)
    assert (s["total"], s["completed"], s["active"], s["failed"], s["awaiting"]) == (4, 2, 1, 1, 1)
    assert s["success_rate"] == "50%" and s["avg"] == "1.0 min" and s["timed"] == 2


def test_duration_falls_back_to_timestamps_and_formats() -> None:
    run = {"created_at": "2026-10-24T10:00:00Z", "finished_at": "2026-10-24T10:02:00Z"}
    assert core.duration_seconds(run) == 120
    assert core.duration_seconds({"created_at": "x"}) is None
    assert [core.fmt_duration(x) for x in (None, 0.2, 42, 600, 7200)] == ["–", "< 1 s", "42 s", "10.0 min", "2.0 h"]


def test_status_badges_tags_and_filter() -> None:
    assert core.status_badge("completed") == ("Completed", "b-ok")
    assert core.status_badge("waiting_approval")[0] == "Needs approval"
    assert core.status_badge("running")[0] == "In Progress"
    assert core.status_badge("stopped")[1] == "b-bad"
    run = {**_run("a", cites=3, mode="live", atts=["pdf", "image"]), "approval_mode": True}
    assert core.run_tags(run) == ["Live", "PDF", "Image", "Step approval", "3 sources"]
    runs = [run, _run("bb", "failed")]
    assert [r["run_id"] for r in core.filter_runs(runs, "pdf")] == ["a"]
    assert [r["run_id"] for r in core.filter_runs(runs, "", "Failed")] == ["bb"]
    assert core.filter_runs(runs, "nothing-matches") == []


def test_sources_deduplicate_across_runs_and_knowledge_counts() -> None:
    runs = [_run("a", cites=2, atts=["pdf"]), _run("b", cites=3, atts=["image"]), _run("c", "running")]
    rows = core.source_rows(runs)
    assert len(rows) == 3 and rows[0]["runs"] == ["a", "b"] and rows[0]["domain"] == "site0.org"
    assert core.source_kind(rows[0]) == "Web" and core.source_kind({"kind": "pdf"}) == "Documents"
    kb = core.knowledge(runs)
    assert (len(kb["reports"]), len(kb["claims"]), kb["web"], kb["documents"], kb["images"]) == (2, 2, 3, 1, 1)


def test_tool_catalog_is_honest_about_mode_keys_and_unsupported() -> None:
    off = {t["name"]: t["state"] for t in core.tool_catalog(Settings(research_mode="offline"), None, "offline")}
    assert off["Web Search"] == "live mode" and off["Video Analysis"] == "coming soon"
    assert off["Document Analysis"] == "on" and off["Image Analysis"] == "needs key"
    keys = {"keys": {"OPENROUTER_API_KEY": {"configured": True, "masked": "****abcd"}}}
    live = {t["name"]: t["state"] for t in core.tool_catalog(Settings(), keys, "live")}
    assert live["Web Search"] == "on" and live["Image Analysis"] == "on" and live["LLM Agents"] == "on"


def test_prefs_roundtrip_and_validation(tmp_path: Path) -> None:
    prefs = core.Prefs(tmp_path / "p" / "prefs.json")
    assert prefs.load() == core.Prefs.DEFAULTS
    assert prefs.save(default_mode="live", list_limit=7, junk=1)["default_mode"] == "live"
    assert prefs.load()["list_limit"] == 7 and "junk" not in prefs.load()
    with pytest.raises(ValueError):
        prefs.save(default_mode="turbo")
    (tmp_path / "p" / "prefs.json").write_text("{bad", encoding="utf-8")
    assert prefs.load() == core.Prefs.DEFAULTS


def test_attachment_from_url_files_pages_and_ssrf() -> None:
    pdf = (ROOT / "examples/demo-data/data/field_note.pdf").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, content=pdf, headers={"content-type": "application/pdf"})
        if request.url.path == "/404":
            return httpx.Response(404)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        if request.url.path == "/zip":
            return httpx.Response(200, content=b"PK", headers={"content-type": "application/zip"})
        return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html; charset=utf-8"})

    tr = httpx.MockTransport(handler)

    def public(u: str) -> bool:
        return "127.0.0.1" not in u and "localhost" not in u

    att = core.attachment_from_url("https://ex.org/doc/report.pdf?x=1", transport=tr, validator=public)
    assert att["kind"] == "pdf" and att["name"] == "report.pdf" and att["data"].startswith("data:application/pdf;base64,")
    page = core.attachment_from_url("https://ex.org/page", transport=tr, validator=public)
    assert page == {"kind": "url", "name": "https://ex.org/page", "data": "https://ex.org/page"}
    with pytest.raises(ValueError, match="404"):
        core.attachment_from_url("https://ex.org/404", transport=tr, validator=public)
    with pytest.raises(ValueError, match="private"):
        core.attachment_from_url("https://ex.org/hop", transport=tr, validator=public)
    with pytest.raises(ValueError, match="private"):
        core.attachment_from_url("http://localhost.test/x", transport=tr, validator=public)
    with pytest.raises(ValueError, match="unsupported"):
        core.attachment_from_url("https://ex.org/zip", transport=tr, validator=public)
    with pytest.raises(ValueError, match="https"):
        core.attachment_from_url("ftp://ex.org/a.pdf", transport=tr)

def test_md_safe_needs_detail_and_ist_display(monkeypatch: pytest.MonkeyPatch) -> None:
    assert core.md_safe("from $4,000 to $7,000") == r"from \$4,000 to \$7,000"
    assert core.md_safe(r"already \$5") == r"already \$5"
    assert core.needs_detail({"status": "completed"}) and not core.needs_detail({"status": "completed", "result": {}})
    monkeypatch.delenv("DEEP_RESEARCH_DISPLAY_TZ", raising=False)
    assert local_time("2026-10-24T17:12:00Z") == "2026-10-24 22:42 IST"


def test_app_title_everywhere_and_no_short_name() -> None:
    src = (ROOT / "src/deep_research/dashboard/app.py").read_text(encoding="utf-8")
    assert core.APP_TITLE == "Multimodal Agentic Deep Research Assistant"
    assert "page_title=APP_TITLE" in src and "MDRA" not in src
    for label in ("Dashboard", "New Research", "My Researches", "Knowledge Base", "Sources", "Tools & Integration", "Settings"):
        assert f'"{label}"' in src


def test_live_feed_splits_sources_and_terminal_lines() -> None:
    from deep_research.dashboard.core import live_feed

    logs = [
        {"seq": 0, "at": "2026-10-24T06:00:00+00:00", "level": "info", "message": "run started"},
        {"seq": 1, "at": "2026-10-24T06:00:01+00:00", "level": "info", "message": "search: ocean co2",
         "event": "search", "query": "ocean co2"},
        {"seq": 2, "at": "2026-10-24T06:00:02+00:00", "level": "info", "message": "source: A <https://www.noaa.gov/a>",
         "event": "source", "url": "https://www.noaa.gov/a", "title": "A", "snippet": "s"},
        {"seq": 3, "at": "2026-10-24T06:00:03+00:00", "level": "info", "message": "source: B", "event": "source", "url": "", "title": "B"},
        {"seq": 4, "at": "2026-10-24T06:00:04+00:00", "level": "info", "message": "evaluator: COMPLETE", "event": "verdict"},
    ]
    feed = live_feed(logs)
    assert feed["sources"] == 2 and feed["queries"] == 1
    assert [c["title"] for c in feed["cards"]] == ["B", "A"]  # newest first
    assert feed["cards"][1]["domain"] == "noaa.gov" and feed["cards"][0]["domain"] == "attached file"
    assert [x["tag"] for x in feed["lines"]] == ["STEP", "SEARCH", "FETCH", "FETCH", "EVAL"]
    assert feed["lines"][0]["time"] == "11:30:00"  # IST
    assert len(live_feed(logs * 30, max_lines=10, max_cards=3)["cards"]) == 3


def test_control_gate_trace_logs_real_events() -> None:
    from deep_research.control.service import ControlGate

    got: list[tuple[str, dict]] = []

    class FakeRun:
        def log(self, message: str, **extra: object) -> None:
            got.append((message, dict(extra)))

    ControlGate(FakeRun()).trace([  # type: ignore[arg-type]
        {"event": "plan", "task": "Find data", "tool": "web_search"},
        {"event": "search", "query": "q1"},
        {"event": "source", "url": "https://x.org/p", "title": "Paper", "snippet": ""},
        {"event": "verdict", "decision": "CONTINUE", "gaps": ["more data"]},
    ])
    msgs = [m for m, _ in got]
    assert msgs == ["plan task: Find data [web_search]", "search: q1", "source: Paper <https://x.org/p>",
                    "evaluator: CONTINUE (gaps: more data)"]
    assert got[2][1]["url"] == "https://x.org/p" and "gaps" not in got[3][1]


def test_orchestrator_source_events_from_tool_result() -> None:
    from types import SimpleNamespace

    from deep_research.core.orchestration.master_research_orchestrator import MasterResearchOrchestrator

    result = SimpleNamespace(metadata={"queries": ["a", "b"]},
                             output={"results": [{"url": "https://e.org", "title": "E"}, {"title": "no url"}],
                                     "source_objects": [SimpleNamespace(url=None, title="report.pdf")]})
    ev = MasterResearchOrchestrator._source_events(result)
    assert [x["event"] for x in ev] == ["search", "search", "source", "source", "source"]
    assert ev[2]["title"] == "report.pdf" and ev[2]["snippet"] == "attached file" and ev[3]["url"] == "https://e.org"
