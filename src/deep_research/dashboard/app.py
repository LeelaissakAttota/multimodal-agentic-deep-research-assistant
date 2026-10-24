"""MDRA dashboard: `streamlit run src/deep_research/dashboard/app.py --server.port 8502`.

Talks to the supervised-control API on 127.0.0.1:8711 (DEEP_RESEARCH_CONTROL_TOKEN).
"""

from __future__ import annotations

import html
import os
import time
from pathlib import Path
from typing import Any

import streamlit as st

from deep_research.core.config import Settings
from deep_research.dashboard.core import (
    TERMINAL, claims, local_time, preview_text, ControlClient, ControlError, RunStore, build_attachment, citations, merge_runs,
    provider_health, report_of, run_stats, shorten, stage_rows, title_of,
)

PAGES = ["New Research", "Live Run", "Report Viewer", "All Runs", "Provider Health", "Settings"]
ROOT = Path(os.environ.get("DEEP_RESEARCH_DASHBOARD_DIR") or Path.cwd() / "dashboard_runs")
EXAMPLE_Q = "What are the main benefits and risks of small modular nuclear reactors?"

st.set_page_config(page_title="MDRA · Deep Research", page_icon="🔬", layout="wide")

CSS = """
<style>
html, body, [data-testid="stAppViewContainer"] { background:#0e1117; }
[data-testid="stSidebar"] { background:#161a23; border-right:1px solid #262b36; }
.block-container { padding-top:2.2rem; padding-bottom:3rem; max-width:1400px; }
* { overflow-wrap:anywhere; }
h1 { font-size:1.7rem !important; margin-bottom:.1rem !important; }
.sub { color:#8b93a7; margin:0 0 1.2rem 0; }
.card { border:1px solid #2f6fd6; background:#141925; border-radius:10px; padding:16px 18px; margin-bottom:18px; }
.card h4 { margin:0 0 10px 0; font-size:1rem; color:#e6e9ef; }
.stats { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:14px; margin:6px 0 20px 0; }
.stat { border:1px solid #2f6fd6; background:#141925; border-radius:10px; padding:14px 16px; height:92px;
        display:flex; flex-direction:column; justify-content:center; min-width:0; }
.stat .l { color:#8b93a7; font-size:.72rem; letter-spacing:.06em; text-transform:uppercase; }
.stat .v { color:#e6e9ef; font-size:1.35rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.stat .s { color:#8b93a7; font-size:.75rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.st-row { display:flex; gap:12px; align-items:center; padding:9px 0; border-bottom:1px solid #222836; min-width:0; }
.dot { width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center;
       font-weight:700; flex:none; }
.d-ok { background:#14532d; color:#4ade80; } .d-wait { background:#78350f; color:#fbbf24; }
.d-bad { background:#7f1d1d; color:#fca5a5; } .d-idle { background:#1e2533; color:#8b93a7; }
.d-run { background:#1e3a8a; color:#93c5fd; }
.muted { color:#8b93a7; font-size:.8rem; }
.cit { color:#93c5fd; font-weight:600; white-space:nowrap; flex:none; min-width:2.2em; }
.note { border-left:3px solid #3b82f6; background:#121a2b; padding:10px 14px; border-radius:6px; color:#c9cfdb;
        font-size:.88rem; margin:4px 0 18px 0; }
.pill { display:inline-block; padding:2px 9px; border-radius:99px; font-size:.72rem; font-weight:600; }
.p-ok { background:#14532d; color:#4ade80; } .p-off { background:#1e2533; color:#8b93a7; }
.p-down { background:#7f1d1d; color:#fca5a5; } .p-wait { background:#78350f; color:#fbbf24; }
.ellipsis { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.st-key-report h1, .st-key-report h2 { font-size:1.25rem !important; margin-top:.6rem !important; }
.st-key-report h3 { font-size:1.05rem !important; }
.brand { font-size:1.15rem; font-weight:700; color:#e6e9ef; } .brand-s { color:#8b93a7; font-size:.78rem; }
div[data-testid="stSidebar"] div[role="radiogroup"] label { padding:6px 8px; border-radius:8px; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)
e = html.escape


@st.cache_resource
def client() -> ControlClient:
    return ControlClient()


store = RunStore(ROOT)


def api_ok() -> bool:
    try:
        client().health()
        return True
    except ControlError:
        return False


def header(title: str, sub: str) -> None:
    st.markdown(f"<h1>{e(title)}</h1><p class='sub'>{e(sub)}</p>", unsafe_allow_html=True)


def stats(items: list[tuple[str, str, str]]) -> None:
    body = "".join(f"<div class='stat'><div class='l'>{e(a)}</div><div class='v' title='{e(b)}'>{e(b)}</div>"
                   f"<div class='s'>{e(c)}</div></div>" for a, b, c in items)
    st.markdown(f"<div class='stats'>{body}</div>", unsafe_allow_html=True)


def dot_class(state: str) -> str:
    return {"succeeded": "d-ok", "approved": "d-ok", "pending_approval": "d-wait", "running": "d-run",
            "failed": "d-bad", "rejected": "d-bad"}.get(state, "d-idle")


def fetch(run_id: str) -> dict[str, Any] | None:
    try:
        run = client().get_run(run_id)
    except ControlError:
        return store.get(run_id)
    store.save(run)
    return run


def all_runs() -> list[dict[str, Any]]:
    try:
        live = client().list_runs()
    except ControlError:
        live = []
    return merge_runs(live, store.list())


def go(page: str, run_id: str | None = None) -> None:
    if run_id:
        st.session_state.run_id = run_id
    st.session_state.goto = page
    st.rerun(scope="app")


# ----- sidebar ----------------------------------------------------------------------
with st.sidebar:
    st.markdown("<div class='brand'>🔬 MDRA</div><div class='brand-s'>Deep Research Assistant · v1.0</div>",
                unsafe_allow_html=True)
    st.write("")
    if st.session_state.get("goto"):
        st.session_state.nav = st.session_state.pop("goto")
    page = st.radio("Navigate", PAGES, key="nav", label_visibility="collapsed")
    st.write("")
    ok = api_ok()
    st.markdown(f"<div class='muted'>Control API :8711 "
                f"<span class='pill {'p-ok' if ok else 'p-down'}'>{'online' if ok else 'offline'}</span><br>"
                f"Free-model guard <span class='pill p-ok'>on</span></div>", unsafe_allow_html=True)


# ----- pages ---------------------------------------------------------------------------
def page_new() -> None:
    header("New Research", "Ask a question and optionally attach a PDF or an image. Every model call is limited to :free models.")
    left, right = st.columns([2, 1], gap="large")
    with left, st.container(border=True):
        q = st.text_area("Research question", key="q", height=110, placeholder=EXAMPLE_Q)
        c1, c2 = st.columns(2)
        pdf = c1.file_uploader("PDF (optional)", type=["pdf"])
        img = c2.file_uploader("Image (optional)", type=["png", "jpg", "jpeg", "webp", "gif"])
        c3, c4 = st.columns(2)
        mode = c3.selectbox("Research mode", ["offline", "live"],
                            format_func=lambda m: "Offline demo (no network)" if m == "offline" else "Live (free web + models)")
        step = c4.toggle("Step-by-step approval", value=True, key="stepmode")
        st.markdown("<div class='note'><b>Step-by-step mode:</b> the run pauses before every stage "
                    "(Plan, Research, Analyze, Evaluate, Report). You see what it will do and press "
                    "<b>Approve</b> or <b>Reject</b>. Turn it off to run straight through.</div>", unsafe_allow_html=True)
        b1, b2, _ = st.columns([1, 1, 2])
        start = b1.button("▶ Start research", type="primary", use_container_width=True)
        b2.button("Load example", use_container_width=True, on_click=lambda: st.session_state.update(q=EXAMPLE_Q))
        if start:
            if not q.strip():
                st.error("Please type a research question.")
                return
            try:
                atts = [build_attachment(f.name, f.getvalue()) for f in (pdf, img) if f is not None]
                client().update_settings(approval_mode=step, auto_approve_low_risk=False)
                run = client().create_run(q.strip(), mode=mode, attachments=atts)
            except (ValueError, ControlError) as exc:
                st.error(str(exc))
                return
            store.save(run)
            go("Live Run", run["run_id"])
    with right:
        runs = all_runs()
        done = sum(r.get("status") == "completed" for r in runs)
        failed = sum(r.get("status") in ("failed", "stopped") for r in runs)
        s = Settings()
        limits = f"{s.max_model_calls_total} model calls · {int(s.max_research_time_seconds)} s limit"
        for label, value, sub in (("Budget", f"{s.max_tool_calls_total} tool calls", limits),
                                  ("Model", shorten(s.openrouter_model, 34), "auto-selected free model in live mode"),
                                  ("Runs", str(len(runs)), f"{done} complete · {failed} failed or stopped")):
            st.markdown(f"<div class='stat' style='margin-bottom:14px'><div class='l'>{e(label)}</div>"
                        f"<div class='v' title='{e(value)}'>{e(value)}</div><div class='s'>{e(sub)}</div></div>",
                        unsafe_allow_html=True)


def pick_run(label: str) -> dict[str, Any] | None:
    runs = all_runs()
    if not runs:
        st.info("No runs yet. Start one on the New Research page.")
        return None
    ids = [r["run_id"] for r in runs]
    cur = st.session_state.get("run_id")
    idx = ids.index(cur) if cur in ids else 0
    names = {r["run_id"]: f"{r['run_id'][:8]} · {r.get('status')} · {title_of(r)[:60]}" for r in runs}
    rid = st.selectbox(label, ids, index=idx, format_func=lambda i: names[i])
    st.session_state.run_id = rid
    return fetch(rid)


def page_live() -> None:
    run = pick_run("Run")
    if run is None:
        return
    live_view(run["run_id"])


@st.fragment(run_every=2)
def live_view(run_id: str) -> None:  # noqa: C901 - one screen of layout
    run = fetch(run_id) or {}
    status = str(run.get("status", ""))
    header(f"Live Run · {run_id[:8]}", title_of(run))
    st_ = run_stats(run)
    pending = len(run.get("pending_approvals") or [])
    stats([("Status", st_["status"], "waiting for you" if pending else ("finished" if status in TERMINAL else "working")),
           ("Steps", st_["steps"], f"{pending} awaiting approval"),
           ("Calls used", st_["calls"], "tool + model calls"),
           ("Elapsed", st_["elapsed"], "wall-clock time")])
    left, right = st.columns([1.15, 1], gap="large")
    with left, st.container(border=True):
        st.markdown("#### Pipeline stages")
        if run.get("approval_mode"):
            st.markdown("<div class='note'>Step-by-step mode is on: each stage waits for your Approve or Reject.</div>",
                        unsafe_allow_html=True)
        rows = stage_rows(run.get("steps") or [])
        if not rows:
            st.markdown("<div class='muted'>Waiting for the first stage…</div>", unsafe_allow_html=True)
        for r in rows:
            a, b = st.columns([3, 2], vertical_alignment="center")
            a.markdown(f"<div class='st-row'><div class='dot {dot_class(r.state)}'>{e(r.icon)}</div>"
                       f"<div style='min-width:0'><b>{r.index + 1}. {e(r.label)}</b> <span class='muted'>· {e(r.agent)}</span>"
                       f"<div class='muted ellipsis' title='{e(r.detail)}'>{e(r.detail)}</div></div></div>",
                       unsafe_allow_html=True)
            if r.awaiting:
                x, y = b.columns(2)
                if x.button("Approve", key=f"ap{r.step_id}", type="primary", use_container_width=True):
                    act(lambda: client().decide(run_id, r.step_id, "approve"))
                if y.button("Reject", key=f"rj{r.step_id}", use_container_width=True):
                    act(lambda: client().decide(run_id, r.step_id, "reject"))
            else:
                b.markdown(f"<span class='pill {'p-ok' if r.state in ('succeeded', 'approved') else 'p-off'}'>"
                           f"{e(r.state.replace('_', ' '))}</span>", unsafe_allow_html=True)
        if status not in TERMINAL:
            st.write("")
            p1, p2, p3 = st.columns(3)
            if p1.button("⏸ Pause", use_container_width=True):
                act(lambda: client().lifecycle(run_id, "pause"))
            if p2.button("▶ Resume", use_container_width=True):
                act(lambda: client().lifecycle(run_id, "resume"))
            if p3.button("■ Stop", use_container_width=True):
                act(lambda: client().lifecycle(run_id, "stop"))
        if run.get("error"):
            st.error(shorten(str(run["error"]), 300))
    with right, st.container(border=True):
        st.markdown("#### Report preview")
        report = report_of(run)
        if report:
            text = preview_text(str(report.get("content", ""))) or (
                f"{len(claims(report))} claims from {len(report.get('evidence_gathered') or [])} evidence items. "
                "Open the full report to read them.")
            st.markdown(f"<div style='color:#c9cfdb;line-height:1.6'>{e(text)}</div>", unsafe_allow_html=True)
            st.write("")
            cits(report, limit=6)
            if st.button("Open full report →", type="primary"):
                go("Report Viewer", run_id)
        else:
            st.markdown("<div class='muted'>The report appears here when the Report stage finishes.</div>",
                        unsafe_allow_html=True)


def act(fn: Any) -> None:
    try:
        fn()
    except ControlError as exc:
        st.error(str(exc))
        return
    time.sleep(0.3)
    st.rerun()


def cits(report: dict[str, Any], limit: int | None = None) -> None:
    items = citations(report)
    if not items:
        return
    st.markdown("**Citations**")
    rows = []
    for c in items[:limit]:
        where = c["url"] or ("uploaded file" + (f", p.{c['page']}" if c.get("page") else ""))
        link = f"<a href='{e(c['url'])}' target='_blank'>{e(shorten(where, 70))}</a>" if c["url"] else e(where)
        rows.append(f"<div class='st-row'><span class='cit'>[{e(str(c['number']))}]</span>"
                    f"<div style='min-width:0'>{e(c['title'])}<div class='muted ellipsis'>{link}</div></div></div>")
    st.markdown("".join(rows), unsafe_allow_html=True)


def page_report() -> None:
    run = pick_run("Report")
    if run is None:
        return
    report = report_of(run)
    header("Report Viewer", title_of(run))
    if not report:
        st.info("This run has no report yet. Finish it on the Live Run page.")
        return
    s = run_stats(run)
    stats([("Status", s["status"], "run outcome"), ("Citations", str(len(citations(report))), "numbered sources"),
           ("Evidence", s["evidence"], "items gathered"), ("Elapsed", s["elapsed"], f"{s['calls']} calls")])
    left, right = st.columns([2, 1], gap="large")
    with left, st.container(border=True, key="report"):
        st.markdown(str(report.get("content", "")))
        cl = claims(report)
        if cl:
            st.markdown("#### Claims")
            st.markdown("".join(
                f"<div class='st-row'><div style='min-width:0;flex:1'>{e(c['text'])}</div>"
                f"<span class='pill p-ok'>{e(c['confidence'])}</span>"
                f"<span class='muted' style='white-space:nowrap'>{c['evidence']} evidence</span></div>" for c in cl[:12]),
                unsafe_allow_html=True)
    with right, st.container(border=True):
        if not citations(report):
            st.markdown("<div class='muted'>No numbered citations in this report. Offline demo runs use "
                        "built-in sample evidence; live runs cite real web pages and your PDF pages.</div>",
                        unsafe_allow_html=True)
            st.write("")
        cits(report)
        st.download_button("⬇ Download Markdown", str(report.get("content", "")),
                           file_name=f"report-{run['run_id'][:8]}.md", use_container_width=True)


def page_runs() -> None:
    header("All Runs", "Every run started from this dashboard. Saved to dashboard_runs/ so the list survives restarts.")
    runs = all_runs()
    if not runs:
        st.info("No runs yet.")
        return
    done = sum(r.get("status") == "completed" for r in runs)
    stats([("Total", str(len(runs)), "runs saved"), ("Completed", str(done), "with a report"),
           ("Failed / stopped", str(sum(r.get("status") in ("failed", "stopped") for r in runs)), "see Live Run for details"),
           ("Active", str(sum(r.get("status") not in TERMINAL for r in runs)), "running or waiting")])
    with st.container(border=True):
        h = st.columns([1.2, 4, 1.4, 1.6, 1.3])
        for col, t in zip(h, ["Run", "Question", "Status", "Started", ""], strict=True):
            col.markdown(f"<span class='muted'><b>{t}</b></span>", unsafe_allow_html=True)
        for r in runs[:50]:
            c = st.columns([1.2, 4, 1.4, 1.6, 1.3], vertical_alignment="center")
            c[0].markdown(f"`{r['run_id'][:8]}`")
            c[1].markdown(f"<div class='ellipsis' title='{e(title_of(r))}'>{e(title_of(r))}</div>", unsafe_allow_html=True)
            stt = str(r.get("status"))
            pill = "p-ok" if stt == "completed" else "p-down" if stt in ("failed", "stopped") else "p-wait"
            c[2].markdown(f"<span class='pill {pill}'>{e(stt.replace('_', ' '))}</span>", unsafe_allow_html=True)
            c[3].markdown(f"<span class='muted'>{e(local_time(r.get('created_at')))}</span>",
                          unsafe_allow_html=True)
            if c[4].button("Open", key=f"o{r['run_id']}", use_container_width=True):
                go("Report Viewer" if stt == "completed" else "Live Run", r["run_id"])


def page_health() -> None:
    header("Provider Health", "Which providers this project can use right now. Key values are never shown, only whether they are set.")
    ok = api_ok()
    try:
        keys = client().keys() if ok else None
    except ControlError:
        keys = None
    rows = provider_health(Settings(), keys, ok)
    good = sum(r["state"] == "ok" for r in rows)
    s = Settings()
    stats([("Healthy", f"{good} / {len(rows)}", "providers ready"),
           ("Research mode", s.research_mode, "default from .env"),
           ("Search", s.search_provider, "auto = Tavily, then DuckDuckGo"),
           ("Agent mode", s.agent_mode, "LLM with deterministic fallback")])
    with st.container(border=True):
        for r in rows:
            pill = {"ok": "p-ok", "off": "p-off"}.get(r["state"], "p-down")
            a, b = st.columns([4, 1], vertical_alignment="center")
            a.markdown(f"<div class='st-row'><div style='min-width:0'><b>{e(r['name'])}</b>"
                       f"<div class='muted'>{e(r['detail'])}</div></div></div>", unsafe_allow_html=True)
            b.markdown(f"<span class='pill {pill}'>{e(r['state'])}</span>", unsafe_allow_html=True)


def page_settings() -> None:
    header("Settings", "Approval policy, provider settings and API keys. Keys are write-only and stored outside the repo.")
    ok = api_ok()
    if not ok:
        st.warning("Control API is offline. Start it with scripts/start-dashboard.ps1.")
        return
    s = Settings()
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("#### Approval policy")
            cur = client().settings()
            am = st.toggle("Step-by-step approval (approval mode)", value=cur.get("approval_mode", False))
            lo = st.toggle("Auto-approve low-risk steps", value=cur.get("auto_approve_low_risk", False))
            st.markdown("<div class='note'>Low-risk steps are planning, research, analysis and reporting. "
                        "High-risk or unknown steps always wait for a human.</div>", unsafe_allow_html=True)
            if st.button("Save policy", type="primary"):
                act(lambda: client().update_settings(approval_mode=am, auto_approve_low_risk=lo))
        with st.container(border=True):
            st.markdown("#### Provider")
            for k, v in (("Research mode", s.research_mode), ("Search provider", s.search_provider),
                         ("Text model", s.openrouter_model), ("Vision model", s.openrouter_vision_model),
                         ("Max iterations", str(s.max_research_iterations)),
                         ("Time limit", f"{int(s.max_research_time_seconds)} s")):
                a, b = st.columns([1, 2])
                a.markdown(f"<span class='muted'>{e(k)}</span>", unsafe_allow_html=True)
                b.markdown(f"<div class='ellipsis' title='{e(v)}'>{e(v)}</div>", unsafe_allow_html=True)
            st.markdown("<div class='muted' style='margin-top:8px'>Change these in .env (DEEP_RESEARCH_*); "
                        "the mode can also be picked per run.</div>", unsafe_allow_html=True)
    with right, st.container(border=True):
        st.markdown("#### API keys")
        keys = client().keys().get("keys", {})
        for name, info in keys.items():
            a, b = st.columns([3, 2], vertical_alignment="center")
            a.markdown(f"<b>{e(name)}</b>", unsafe_allow_html=True)
            b.markdown(f"<span class='pill {'p-ok' if info.get('configured') else 'p-off'}'>"
                       f"{e(info.get('masked') or 'not set')}</span>", unsafe_allow_html=True)
        st.write("")
        name = st.selectbox("Key to update", list(keys))
        value = st.text_input("New value", type="password", placeholder="paste the key, it is never shown again")
        if st.button("Save key", type="primary"):
            act(lambda: client().set_key(name, value))
        st.markdown("<div class='muted'>Saved to the local key store (file mode 0600). New keys apply on the next run.</div>",
                    unsafe_allow_html=True)


{"New Research": page_new, "Live Run": page_live, "Report Viewer": page_report, "All Runs": page_runs,
 "Provider Health": page_health, "Settings": page_settings}[page]()
