# ruff: noqa: E501  (inline HTML/CSS templates)
"""Multimodal Agentic Deep Research Assistant dashboard.

Run: ``streamlit run src/deep_research/dashboard/app.py --server.port 8502``. It talks to the
supervised-control API on 127.0.0.1:8711 (DEEP_RESEARCH_CONTROL_TOKEN). Every number shown comes
from that API or from runs saved under ``dashboard_runs/``; nothing is invented.
"""

from __future__ import annotations

import html
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import streamlit as st

from deep_research.core.config import Settings
from deep_research.dashboard.core import (
    APP_TITLE, display_zone, live_feed, md_safe, REPO_URL, TERMINAL, ControlClient, ControlError, Prefs, RunStore, attachment_from_url,
    build_attachment, citations, claims, fmt_duration, duration_seconds, knowledge, local_time, merge_runs,
    needs_detail, preview_text, provider_health, report_charts, report_of, run_stats, run_tags, shorten, source_kind,
    strip_section, verdict_label, verification_line, verification_rows,
    source_rows, stage_rows, status_badge, summary, title_of, tool_catalog, filter_runs,
)

NAV = [("dashboard", "Dashboard", "▦"), ("new", "New Research", "＋"), ("researches", "My Researches", "☰"),
       ("knowledge", "Knowledge Base", "◈"), ("sources", "Sources", "⌘"), ("tools", "Tools & Integration", "⚙"),
       ("settings", "Settings", "⚒")]
ROOT = Path(os.environ.get("DEEP_RESEARCH_DASHBOARD_DIR") or Path.cwd() / "dashboard_runs")
EXAMPLES = [
    ("⚛️", "Small modular reactors", "Benefits, risks and costs of SMRs",
     "What are the main benefits and risks of small modular nuclear reactors?"),
    ("🌾", "Climate and agriculture", "Impact of climate change on crop yields",
     "How is climate change affecting global crop yields, and which adaptations work best?"),
    ("💻", "AI in education", "Evidence on AI tutors and learning outcomes",
     "What does current evidence say about AI tutoring systems and student learning outcomes?"),
    ("🩺", "Health and wellness", "Health effects of intermittent fasting",
     "What are the proven health benefits and risks of intermittent fasting?"),
]

st.set_page_config(page_title=APP_TITLE, page_icon="🔬", layout="wide", initial_sidebar_state="expanded")

LOGO = ("<svg width='30' height='30' viewBox='0 0 32 32' fill='none'><circle cx='16' cy='16' r='3' fill='#60a5fa'/>"
        "<ellipse cx='16' cy='16' rx='13' ry='5' stroke='#60a5fa' stroke-width='1.6'/>"
        "<ellipse cx='16' cy='16' rx='13' ry='5' stroke='#60a5fa' stroke-width='1.6' transform='rotate(60 16 16)'/>"
        "<ellipse cx='16' cy='16' rx='13' ry='5' stroke='#60a5fa' stroke-width='1.6' transform='rotate(120 16 16)'/></svg>")

CSS = """
<style>
:root { --bg:#0a1228; --panel:#0f1a36; --panel2:#13214a; --line:#1f3266; --text:#e6ecff; --mut:#8fa0c8; --blue:#2f6bff; }
html, body, [data-testid="stAppViewContainer"], [data-testid="stMain"] { background:var(--bg) !important; color:var(--text); }
header[data-testid="stHeader"] { display:none; }
[data-testid="stSidebar"] { background:#0b1530 !important; border-right:1px solid var(--line); top:58px !important; height:calc(100vh - 58px) !important; }
[data-testid="stSidebar"] > div:first-child { padding-top:0; }
[data-testid="stSidebarHeader"], [data-testid="stSidebarCollapseButton"] { display:none; }
[data-testid="stSidebarUserContent"] { padding:14px 12px 16px 12px; }
.block-container { padding:76px 28px 40px 28px !important; max-width:1600px; }
* { overflow-wrap:anywhere; box-sizing:border-box; }
a { text-decoration:none !important; }
.topbar { position:fixed; top:0; left:0; right:0; height:58px; z-index:999990; background:#0b1530; border-bottom:1px solid var(--line);
          display:flex; align-items:center; gap:12px; padding:0 22px; }
.topbar .t { font-weight:700; font-size:1.08rem; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; flex:1; }
.topbar .r { display:flex; align-items:center; gap:18px; color:var(--mut); font-size:.8rem; white-space:nowrap; flex:none; }
.on { display:inline-block; width:8px; height:8px; border-radius:50%; background:#22c55e; margin-right:6px; box-shadow:0 0 6px #22c55e; }
.off { display:inline-block; width:8px; height:8px; border-radius:50%; background:#ef4444; margin-right:6px; }
.ico { width:30px; height:30px; border-radius:50%; background:var(--panel2); display:inline-flex; align-items:center; justify-content:center;
       color:var(--text); position:relative; font-size:.9rem; }
.ico .n { position:absolute; top:-4px; right:-4px; background:#ef4444; color:#fff; border-radius:9px; font-size:.6rem; padding:0 5px; }
.nav a { display:flex; align-items:center; gap:10px; padding:9px 12px; border-radius:8px; color:#c3cdea !important; font-size:.88rem; margin-bottom:3px; }
.nav a:hover { background:var(--panel2); }
.nav a.act { background:var(--blue); color:#fff !important; font-weight:600; }
.nav .i { width:18px; text-align:center; opacity:.9; }
.proj { border:1px solid var(--line); background:var(--panel); border-radius:10px; padding:12px; margin-top:22px; font-size:.78rem; color:var(--mut); }
.proj b { color:var(--text); font-size:.82rem; }
.bar { height:6px; background:#1b2a55; border-radius:4px; overflow:hidden; margin:6px 0 4px 0; }
.bar > div { height:100%; background:linear-gradient(90deg,#2f6bff,#60a5fa); }
h1.pt { font-size:1.45rem !important; margin:0 0 2px 0 !important; padding:0 !important; color:var(--text); }
.sub { color:var(--mut); margin:0 0 18px 0; font-size:.88rem; }
.panel { border:1px solid var(--line); background:var(--panel); border-radius:12px; padding:16px 18px; margin-bottom:16px; min-width:0; }
.panel h4, .ph { margin:0 0 12px 0; font-size:.98rem; color:var(--text); font-weight:650; display:flex; justify-content:space-between; align-items:center; }
.ph a { font-size:.75rem; color:#60a5fa !important; font-weight:500; }
.banner { border:1px solid #2a4aa0; background:linear-gradient(110deg,#11245a 0%,#0f1a36 60%,#132a66 100%); border-radius:12px;
          padding:20px 22px; display:flex; gap:18px; align-items:center; margin-bottom:16px; min-height:112px; }
.banner .bt { font-size:1.25rem; font-weight:700; color:var(--text); margin-bottom:4px; }
.banner .bs { color:#b7c4e8; font-size:.86rem; max-width:620px; }
.banner .side { margin-left:auto; border:1px solid #2a4aa0; background:#0d1a40; border-radius:10px; padding:12px 14px; width:260px; flex:none; font-size:.78rem; color:var(--mut); }
.btn { display:inline-block; background:var(--blue); color:#fff !important; padding:7px 14px; border-radius:8px; font-size:.8rem; font-weight:600; margin-top:8px; }
.btn.g { background:transparent; border:1px solid var(--line); color:#c3cdea !important; }
.tiles { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-bottom:16px; }
.tile { border:1px solid var(--line); background:var(--panel); border-radius:12px; padding:14px 16px; height:104px; display:flex; gap:14px; align-items:center; min-width:0; }
.tile .tx { min-width:0; flex:1; }
.tile .l { color:var(--mut); font-size:.78rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tile .v { color:var(--text); font-size:1.6rem; font-weight:750; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.tile .s { font-size:.74rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#4ade80; }
.tile .s.m { color:var(--mut); }
.sq { width:42px; height:42px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:1.15rem; flex:none; }
.c1 { background:#1d3b8f; } .c2 { background:#14532d; } .c3 { background:#7c2d12; } .c4 { background:#4c1d95; } .c5 { background:#164e63; } .c6 { background:#7f1d1d; }
.row { display:flex; gap:12px; align-items:center; padding:10px 4px; border-bottom:1px solid #1a2a57; min-width:0; color:var(--text) !important; }
a.row:hover { background:#13214a; border-radius:8px; }
.row:last-child { border-bottom:none; }
.row .tx { min-width:0; flex:1; }
.row .tt { font-weight:600; font-size:.88rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .ts { color:var(--mut); font-size:.75rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.row .rt { text-align:right; flex:none; font-size:.72rem; color:var(--mut); white-space:nowrap; }
.badge { display:inline-block; padding:2px 9px; border-radius:6px; font-size:.7rem; font-weight:650; white-space:nowrap; }
.b-ok { background:#14532d; color:#4ade80; } .b-bad { background:#7f1d1d; color:#fca5a5; } .b-wait { background:#78350f; color:#fbbf24; }
.b-run { background:#1e3a8a; color:#93c5fd; } .b-off { background:#1b2a55; color:var(--mut); } .b-soon { background:#3b2a5c; color:#c4b5fd; }
.tag { display:inline-block; padding:1px 8px; border-radius:5px; font-size:.68rem; background:#1b2a55; color:#a9b8e0; margin:3px 4px 0 0; white-space:nowrap; }
.grid { display:grid; gap:10px; }
.g2 { grid-template-columns:repeat(2,minmax(0,1fr)); } .g3 { grid-template-columns:repeat(3,minmax(0,1fr)); }
.g4 { grid-template-columns:repeat(4,minmax(0,1fr)); } .g6 { grid-template-columns:repeat(6,minmax(0,1fr)); }
.cap { border:1px solid var(--line); background:var(--panel2); border-radius:10px; padding:10px; min-width:0; min-height:96px; }
.cap .ct { font-weight:600; font-size:.8rem; margin-top:6px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.cap .cs { color:var(--mut); font-size:.7rem; line-height:1.3; }
.cap.dis { opacity:.55; }
.qa { display:flex; gap:12px; align-items:center; border:1px solid var(--line); background:var(--panel2); border-radius:10px; padding:11px 12px; margin-bottom:9px; color:var(--text) !important; min-width:0; }
.qa.p { background:var(--blue); border-color:var(--blue); }
.qa .tt { font-weight:600; font-size:.85rem; } .qa .ts { font-size:.72rem; color:#c3cdea; }
.st-row { display:flex; gap:12px; align-items:center; padding:9px 0; border-bottom:1px solid #1a2a57; min-width:0; }
.dot { width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-weight:700; flex:none; }
.d-ok { background:#14532d; color:#4ade80; } .d-wait { background:#78350f; color:#fbbf24; } .d-bad { background:#7f1d1d; color:#fca5a5; }
.d-idle { background:#1b2a55; color:var(--mut); } .d-run { background:#1e3a8a; color:#93c5fd; }
.muted { color:var(--mut); font-size:.8rem; }
.cit { color:#93c5fd; font-weight:600; white-space:nowrap; flex:none; min-width:2.2em; }
.note { border-left:3px solid var(--blue); background:#0d1a40; padding:10px 14px; border-radius:6px; color:#c3cdea; font-size:.84rem; margin:4px 0 14px 0; }
.ellipsis { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.kv { display:flex; justify-content:space-between; gap:12px; padding:9px 0; border-bottom:1px solid #1a2a57; font-size:.85rem; }
.kv span:last-child { color:var(--text); text-align:right; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.kv span:first-child { color:var(--mut); flex:none; }
[data-testid="stVerticalBlockBorderWrapper"] { border-color:var(--line) !important; background:var(--panel); border-radius:12px !important; }
.stTabs [data-baseweb="tab-list"] { gap:6px; }
.stTabs [data-baseweb="tab"] { background:var(--panel2); border-radius:8px; padding:6px 14px; height:auto; }
.stTabs [aria-selected="true"] { background:var(--blue) !important; color:#fff !important; }
.stTabs [data-baseweb="tab-highlight"], .stTabs [data-baseweb="tab-border"] { display:none; }
[data-testid="stFileUploaderDropzone"] { background:#0d1a40; border:1.5px dashed #2a4aa0; }
.st-key-report h1, .st-key-report h2 { font-size:1.2rem !important; } .st-key-report h3 { font-size:1.02rem !important; }
.lucy { display:grid; grid-template-columns:minmax(0,1.1fr) minmax(0,1fr); gap:14px; height:360px; margin-bottom:16px; }
.stack { position:relative; border:1px solid var(--line); background:radial-gradient(circle at 30% 20%,#13244f,#08101f 70%); border-radius:12px; overflow:hidden; min-width:0; }
.stack .hd, .term .hd { position:absolute; top:0; left:0; right:0; padding:8px 12px; font-size:.72rem; color:var(--mut); background:rgba(8,16,31,.85); z-index:50; display:flex; justify-content:space-between; }
.wcard { position:absolute; left:6%; width:80%; background:#f4f7ff; color:#111827; border-radius:8px; padding:10px 12px; box-shadow:0 -6px 18px rgba(0,0,0,.45); min-width:0; overflow:hidden; height:96px; }
.wcard .bar2 { display:flex; gap:4px; margin-bottom:6px; } .wcard .bar2 i { width:7px; height:7px; border-radius:50%; background:#cbd5e1; display:block; }
.wcard .d { font-size:.66rem; color:#2563eb; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.wcard .t { font-size:.8rem; font-weight:700; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.wcard .s { font-size:.68rem; color:#475569; line-height:1.3; max-height:2.6em; overflow:hidden; }
.wcard.new { animation:slidein .45s cubic-bezier(.2,.9,.3,1.2) both; }
@keyframes slidein { from { transform:translate(120%,-30px) rotate(8deg); opacity:0; } to { opacity:1; } }
.term { position:relative; border:1px solid #14532d; background:#020a05; border-radius:12px; overflow:hidden; min-width:0; }
.term .hd { background:rgba(2,10,5,.9); color:#4ade80; border-bottom:1px solid #14532d; }
.term .body { position:absolute; top:32px; bottom:0; left:0; right:0; overflow:hidden; display:flex; flex-direction:column; justify-content:flex-end; padding:8px 12px;
  font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; font-size:.7rem; line-height:1.45; color:#4ade80; }
.term .ln { flex-shrink:0; height:16px; line-height:16px; font-size:11px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; } .term .ln b { color:#86efac; font-weight:600; display:inline-block; width:56px; }
.term .ln.new { animation:typein .35s ease-out both; } @keyframes typein { from { opacity:0; transform:translateY(6px); } to { opacity:1; } }
.term .cur { display:inline-block; width:7px; height:12px; background:#4ade80; animation:blink 1s steps(1) infinite; vertical-align:middle; }
@keyframes blink { 50% { opacity:0; } }
.lucy .empty { position:absolute; top:46%; left:0; right:0; text-align:center; color:var(--mut); font-size:.8rem; }
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)
e = html.escape
Q = st.query_params


@st.cache_resource
def client() -> ControlClient:
    return ControlClient()


store = RunStore(ROOT)
prefs = Prefs(ROOT / "prefs.json")


def api_ok() -> bool:
    try:
        client().health()
        return True
    except ControlError:
        return False


def link(page: str, **extra: str) -> str:
    q = f"?page={page}" + "".join(f"&{k}={quote(v)}" for k, v in extra.items())
    return q


def go(page: str, **extra: str) -> None:
    Q.clear()
    Q.update({"page": page, **extra})
    st.rerun()


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
    runs = merge_runs(live, store.list())
    out = []
    for r in runs:
        if needs_detail(r):
            r = fetch(str(r["run_id"])) or r
        out.append(r)
    return out


def act(fn: Any) -> None:
    try:
        fn()
    except (ControlError, ValueError) as exc:
        st.error(str(exc))
        return
    time.sleep(0.3)
    st.rerun()


def head(title: str, sub: str) -> None:
    st.markdown(f"<h1 class='pt'>{e(title)}</h1><p class='sub'>{e(sub)}</p>", unsafe_allow_html=True)


def tiles(items: list[tuple[str, str, str, str, str, bool]]) -> None:
    body = "".join(
        f"<div class='tile'><div class='sq {c}'>{i}</div><div class='tx'><div class='l'>{e(lab)}</div>"
        f"<div class='v' title='{e(v)}'>{e(v)}</div><div class='s{'' if good else ' m'}' title='{e(s)}'>{e(s)}</div></div></div>"
        for i, c, lab, v, s, good in items)
    st.markdown(f"<div class='tiles'>{body}</div>", unsafe_allow_html=True)


def badge(status: Any) -> str:
    label, cls = status_badge(status)
    return f"<span class='badge {cls}'>{e(label)}</span>"


def run_icon(run: dict[str, Any]) -> str:
    kinds = {str(a.get("kind")) for a in (run.get("params") or {}).get("attachments") or [] if isinstance(a, dict)}
    if "image" in kinds:
        return "<div class='sq c4'>🖼️</div>"
    if "pdf" in kinds:
        return "<div class='sq c3'>📄</div>"
    return "<div class='sq c5'>🌐</div>" if (run.get("params") or {}).get("mode") == "live" else "<div class='sq c1'>🔬</div>"


def run_row(r: dict[str, Any], *, tags: bool = False) -> str:
    view = "report" if r.get("status") == "completed" else "live"
    tg = "".join(f"<span class='tag'>{e(t)}</span>" for t in run_tags(r)) if tags else ""
    sub = tg or e(" · ".join(run_tags(r)))
    return (f"<a class='row' target='_self' href='{link('researches', run=str(r['run_id']), view=view)}'>{run_icon(r)}"
            f"<div class='tx'><div class='tt' title='{e(title_of(r))}'>{e(title_of(r))}</div>"
            f"<div class='ts'>{sub}</div></div><div class='rt'>{badge(r.get('status'))}<br>"
            f"{e(local_time(r.get('created_at')))}</div></a>")


def dot_class(state: str) -> str:
    return {"succeeded": "d-ok", "approved": "d-ok", "pending_approval": "d-wait", "running": "d-run",
            "failed": "d-bad", "rejected": "d-bad"}.get(state, "d-idle")


# ----- chrome: header + sidebar ------------------------------------------------------------------
ok = api_ok()
runs_cache = all_runs()
waiting = sum(bool(r.get("pending_approvals")) for r in runs_cache if r.get("status") not in TERMINAL)
now = datetime.now().astimezone(display_zone())
zone = "IST" if (now.utcoffset() or 0) and now.utcoffset().total_seconds() == 19800 else now.tzname()  # type: ignore[union-attr]
st.markdown(
    f"<div class='topbar'>{LOGO}<div class='t'>{e(APP_TITLE)}</div><div class='r'>"
    f"<span><span class='{'on' if ok else 'off'}'></span>{'System Online' if ok else 'Control API offline'}</span>"
    f"<span>{now:%b %d, %Y · %H:%M} {e(zone or '')}</span>"
    f"<a class='ico' target='_self' href='{link('researches', status='Needs approval')}' title='Runs waiting for approval'>🔔"
    f"{f'<span class=n>{waiting}</span>' if waiting else ''}</a>"
    f"<a class='ico' target='_self' href='{link('settings', tab='account')}' title='Account and API keys'>👤</a></div></div>",
    unsafe_allow_html=True)

page = Q.get("page", "dashboard")
if page not in {k for k, _, _ in NAV}:
    page = "dashboard"
tools_now = tool_catalog(Settings(), None, prefs.load()["default_mode"])
shipped = sum(t["state"] != "coming soon" for t in tools_now)
pct = round(100 * shipped / len(tools_now))
with st.sidebar:
    st.markdown("<div class='nav'>" + "".join(
        f"<a target='_self' class='{'act' if k == page else ''}' href='{link(k)}'><span class='i'>{i}</span>{e(lab)}</a>"
        for k, lab, i in NAV) + "</div>", unsafe_allow_html=True)
    st.markdown(
        f"<div class='proj'><div style='display:flex;gap:10px;align-items:center'><div class='sq c1' style='width:34px;height:34px'>🔬</div>"
        f"<div style='min-width:0'><span class='muted'>Project</span><br><b>Deep Research Assistant</b></div></div>"
        f"<div style='margin-top:8px'><span class='badge b-ok'>Phase: v{e(Settings().version)} released</span></div>"
        f"<div style='margin-top:10px;display:flex;justify-content:space-between'><span>Overall progress</span><b>{pct}%</b></div>"
        f"<div class='bar'><div style='width:{pct}%'></div></div>"
        f"<div>{shipped} of {len(tools_now)} capabilities shipped</div>"
        f"<div style='margin-top:10px'>Project repository<br><a href='{REPO_URL}' target='_blank' style='color:#60a5fa'>"
        f"View on GitHub ↗</a></div></div>", unsafe_allow_html=True)


# ----- Dashboard -----------------------------------------------------------------------------------
def page_dashboard() -> None:
    runs = runs_cache
    s = summary(runs)
    st.markdown(
        f"<div class='banner'><div class='sq c1' style='width:64px;height:64px;font-size:1.8rem'>🔭</div><div>"
        f"<div class='bt'>Welcome to your Deep Research Assistant</div><div class='bs'>Plan, search, read PDFs and images, "
        f"and get cited reports. Every step can wait for your approval, and only free models are used.</div>"
        f"<a class='btn' target='_self' href='{link('new')}'>＋ Start new research</a></div>"
        f"<div class='side'><b style='color:#e6ecff'>Supervised control</b><br>Control API "
        f"{'online on :8711' if ok else 'offline'}<br>{s['awaiting']} run(s) waiting for approval<br>"
        f"Free-model guard: on</div></div>", unsafe_allow_html=True)
    tiles([("📊", "c1", "Total Researches", str(s["total"]), f"{s['failed']} failed or stopped", False),
           ("✅", "c2", "Completed", str(s["completed"]), f"{s['success_rate']} success rate", s["completed"] > 0),
           ("⏳", "c3", "In Progress", str(s["active"]), f"{s['awaiting']} waiting for approval", False),
           ("⏱️", "c4", "Avg. Completion Time", s["avg"], f"across {s['timed']} completed run(s)", False)])
    left, right = st.columns([1.35, 1], gap="medium")
    with left:
        rows = "".join(run_row(r) for r in runs[:prefs.load()["list_limit"]]) or \
            "<div class='muted'>No researches yet. Start one from New Research.</div>"
        st.markdown(f"<div class='panel'><div class='ph'>Recent Researches <a target='_self' href='{link('researches')}'>"
                    f"View all →</a></div>{rows}</div>", unsafe_allow_html=True)
        kb = knowledge(runs)
        types = [("🌐", "c1", "Web", str(kb["web"]), "cited pages"), ("📄", "c3", "Documents", str(kb["documents"]), "PDFs read"),
                 ("🖼️", "c4", "Images", str(kb["images"]), "images read"), ("🎬", "c6", "Videos", "–", "coming soon")]
        cells = "".join(f"<div class='cap{' dis' if v == '–' else ''}'><div class='sq {c}' style='width:32px;height:32px'>{i}</div>"
                        f"<div class='ct'>{e(n)}</div><div class='cs'>{e(v)} · {e(d)}</div></div>" for i, c, n, v, d in types)
        st.markdown(f"<div class='panel'><div class='ph'>Recent Sources <a target='_self' href='{link('sources')}'>View all →</a></div>"
                    f"<div class='grid g4'>{cells}</div></div>", unsafe_allow_html=True)
    with right:
        cells = "".join(
            f"<div class='cap{' dis' if t['state'] == 'coming soon' else ''}'><div class='sq c1' style='width:32px;height:32px'>{t['icon']}</div>"
            f"<div class='ct' title='{e(t['name'])}'>{e(t['name'])}</div><div class='cs'>"
            f"<span class='badge {'b-ok' if t['state'] == 'on' else 'b-soon' if t['state'] == 'coming soon' else 'b-off'}'>"
            f"{e(t['state'])}</span></div></div>" for t in tool_catalog(Settings(), keys_or_none(), prefs.load()["default_mode"]) if t["name"] != "LLM Agents")
        st.markdown(f"<div class='panel'><div class='ph'>Research Capabilities <a target='_self' href='{link('tools')}'>Details →</a></div>"
                    f"<div class='grid g3'>{cells}</div></div>", unsafe_allow_html=True)
        st.markdown(
            f"<div class='panel'><div class='ph'>Quick Actions</div>"
            f"<a class='qa p' target='_self' href='{link('new')}'><div class='sq' style='background:#1d4ed8'>＋</div><div><div class='tt'>Start New Research</div>"
            f"<div class='ts'>Ask a question, step-by-step approval</div></div></a>"
            f"<a class='qa' target='_self' href='{link('new', tab='upload')}'><div class='sq c3'>⤒</div><div><div class='tt'>Upload Document</div>"
            f"<div class='ts'>PDF or image (max 20 MB)</div></div></a>"
            f"<a class='qa' target='_self' href='{link('knowledge')}'><div class='sq c4'>◈</div><div><div class='tt'>View Knowledge Base</div>"
            f"<div class='ts'>Reports, claims and sources</div></div></a></div>", unsafe_allow_html=True)


def keys_or_none() -> dict[str, Any] | None:
    try:
        return client().keys() if ok else None
    except ControlError:
        return None


# ----- New Research (+ live run / report views) ---------------------------------------------------
def page_new() -> None:
    head("New Research", "Start a new research project. Ask a question, upload files, or give a direct PDF/image link.")
    p = prefs.load()
    left, right = st.columns([1.6, 1], gap="medium")
    with left, st.container(border=True):
        t1, t2, t3 = st.tabs(["💬 Ask a Question", "⤒ Upload Files", "🔗 From URL"])
        with t1:
            q = st.text_area("What would you like to research?", key="q", height=120,
                             placeholder="What would you like to research? Be specific for better results…")
        with t2:
            st.markdown("<div class='note'>Use the drop zone below to attach up to 5 PDF or image files (max 20 MB each). "
                        "They are sent with the question from the first tab. Audio and video uploads are "
                        "<span class='badge b-soon'>coming soon</span>.</div>", unsafe_allow_html=True)
        with t3:
            url = st.text_input("Direct link to a PDF or image", placeholder="https://example.org/report.pdf")
            st.markdown("<div class='muted'>The file is downloaded and attached. Reading arbitrary web pages "
                        "by URL is <span class='badge b-soon'>coming soon</span>; live mode already searches and reads pages.</div>",
                        unsafe_allow_html=True)
        c = st.columns(2)
        web = c[0].toggle("Web Search", value=p["default_mode"] == "live", help="On = live mode (free web search + models). Off = offline demo.")
        c[1].toggle("Document Analysis (PDF)", value=True, disabled=True, help="Always on: attached PDFs are read with page citations.")
        c[0].toggle("Image Analysis", value=True, disabled=True, help="Always on: attached images are analysed.")
        c[1].toggle("Video Analysis (coming soon)", value=False, disabled=True, help="Not supported by the engine yet.")
        files = st.file_uploader("Upload files (optional): drag and drop PDF or image files here",
                                 type=["pdf", "png", "jpg", "jpeg", "webp", "gif"], accept_multiple_files=True)
        a, b = st.columns([1.4, 1], vertical_alignment="center")
        step = a.toggle("Step-by-step approval (approve each stage)", value=p["step_approval"])
        start = b.button("Start Research  →", type="primary", use_container_width=True)
        if start:
            if not str(q).strip():
                st.error("Please type a research question in the first tab.")
                return
            try:
                atts = [build_attachment(f.name, f.getvalue()) for f in (files or [])][:5]
                if url.strip():
                    atts.append(attachment_from_url(url))
                client().update_settings(approval_mode=step, auto_approve_low_risk=False)
                run = client().create_run(str(q).strip(), mode="live" if web else "offline", attachments=atts)
            except (ValueError, ControlError) as exc:
                st.error(str(exc))
                return
            store.save(run)
            go("new", run=str(run["run_id"]), view="live")
    with right:
        st.markdown("<div class='ph' style='margin-top:4px'>Example Research Ideas</div>", unsafe_allow_html=True)
        for i, (ic, t, s, full) in enumerate(EXAMPLES):
            if st.button(f"{ic}  {t}", key=f"ex{i}", help=s, use_container_width=True):
                st.session_state.q = full
                st.rerun()
        active = [r for r in runs_cache if r.get("status") not in TERMINAL]
        rows = "".join(run_row(r) for r in active[:3]) or "<div class='muted'>No run in progress.</div>"
        st.markdown(f"<div class='panel' style='margin-top:10px'><div class='ph'>Runs in progress</div>{rows}</div>",
                    unsafe_allow_html=True)
    st.markdown("<div class='note'><b>Step-by-step mode:</b> the run pauses before every stage (Plan, Research, Analyze, "
                "Evaluate, Report). You see what it will do and press <b>Approve</b> or <b>Reject</b>.</div>", unsafe_allow_html=True)


@st.fragment(run_every=2)
def live_view(run_id: str) -> None:  # noqa: C901 - one screen of layout
    run = fetch(run_id) or {}
    status = str(run.get("status", ""))
    st.markdown(f"<a class='btn g' target='_self' href='{link('researches')}'>← My Researches</a>", unsafe_allow_html=True)
    head(f"Live Run · {run_id[:8]}", title_of(run))
    s = run_stats(run)
    pending = len(run.get("pending_approvals") or [])
    short = {"waiting_approval": "Awaiting", "completed": "Completed"}.get(status, s["status"])
    tiles([("📌", "c1", "Status", short, "waiting for you" if pending else ("finished" if status in TERMINAL else "working"), False),
           ("🧩", "c5", "Steps", s["steps"], f"{pending} awaiting approval", False),
           ("⚡", "c3", "Calls used", s["calls"], "tool + model calls", False),
           ("⏱️", "c4", "Elapsed", fmt_duration(duration_seconds(run)), "wall-clock time", False)])
    research_view(run_id, status)
    left, right = st.columns([1.15, 1], gap="medium")
    with left, st.container(border=True):
        st.markdown("<div class='ph'>Pipeline stages</div>", unsafe_allow_html=True)
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
                       f"<div class='muted ellipsis' title='{e(r.detail)}'>{e(r.detail)}</div></div></div>", unsafe_allow_html=True)
            if r.awaiting:
                x, y = b.columns(2)
                if x.button("Approve", key=f"ap{r.step_id}", type="primary", use_container_width=True):
                    act(lambda: client().decide(run_id, r.step_id, "approve"))
                if y.button("Reject", key=f"rj{r.step_id}", use_container_width=True):
                    act(lambda: client().decide(run_id, r.step_id, "reject"))
            else:
                b.markdown(f"<span class='badge {'b-ok' if r.state in ('succeeded', 'approved') else 'b-off'}'>"
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
        st.markdown("<div class='ph'>Report preview</div>", unsafe_allow_html=True)
        report = report_of(run)
        if report:
            text = preview_text(str(report.get("content", ""))) or f"{len(claims(report))} claims. Open the full report."
            st.markdown(f"<div style='color:#c3cdea;line-height:1.6'>{e(text)}</div>", unsafe_allow_html=True)
            st.write("")
            cits(report, limit=6)
            st.markdown(f"<a class='btn' target='_self' href='{link('researches', run=run_id, view='report')}'>Open full report →</a>",
                        unsafe_allow_html=True)
        else:
            st.markdown("<div class='muted'>The report appears here when the Report stage finishes.</div>", unsafe_allow_html=True)
            st.write("")


def research_view(run_id: str, status: str) -> None:
    """Live Research View: real sources sliding in as stacked windows + a green terminal of real run events."""

    try:
        logs = client().logs(run_id)
    except ControlError:
        logs = []
    feed = live_feed(logs, max_cards=7)
    seen_key = f"seen_{run_id}"
    first = seen_key not in st.session_state
    seen = int(st.session_state.get(seen_key, -1))
    st.session_state[seen_key] = max([seen, *[x["seq"] for x in feed["lines"]]])
    live = status not in TERMINAL
    cards = []
    n = len(feed["cards"])
    for i, c in enumerate(reversed(feed["cards"])):  # oldest first, newest painted on top
        depth = n - 1 - i
        top = 230 - depth * 30
        rot = (-1.6, 1.2, -0.6, 1.8, -1.1, 0.7, -1.9, 1.4)[c["seq"] % 8]
        style = f"top:{top}px;transform:translateX({depth * 2.2}%) rotate({rot}deg);z-index:{40 - depth};filter:brightness({max(0.45, 1 - depth * 0.1):.2f})"
        title = (f"<a href='{e(c['url'])}' target='_blank' style='color:#111827'>{e(c['title'])}</a>" if c["url"].startswith("http")
                 else e(c["title"]))
        cards.append(f"<div class='wcard{' new' if c['seq'] > seen else ''}' style='{style}'><div class='bar2'><i></i><i></i><i></i></div>"
                     f"<div class='d'>{e(c['domain'])}</div><div class='t' title='{e(c['title'])}'>{title}</div>"
                     f"<div class='s'>{e(c['snippet'])}</div></div>")
    lines = "".join(f"<div class='ln{' new' if x['seq'] > seen and not first else ''}' title='{e(x['text'])}'>[{e(x['time'])}] <b>{e(x['tag']):<6}</b> {e(x['text'])}</div>"
                    for x in feed["lines"])
    state = "LIVE" if live else "FINISHED"
    empty = "" if cards else (f"<div class='empty'>{'Waiting for the first source…' if live else ('No web sources were logged for this run.' if logs else 'Source trail not available for this saved run.')}</div>")
    st.markdown(
        f"<div class='ph'>Live Research View <span class='muted'>{feed['sources']} sources · {feed['queries']} searches · real run events only</span></div>"
        f"<div class='lucy'><div class='stack'><div class='hd'><span>SOURCES BEING READ</span><span>{state}</span></div>{''.join(cards)}{empty}</div>"
        f"<div class='term'><div class='hd'><span>agent@deep-research:~$ tail -f run/{e(run_id[:8])}.log</span><span>{state}</span></div>"
        f"<div class='body'>{lines or ('<div class=ln>waiting for events…</div>' if live else '<div class=ln>run log not available here (saved run, or it predates this view)</div>')}"
        f"<div class='ln'>{'<span class=cur></span>' if live else '— run ' + e(status) + ' —'}</div></div></div></div>",
        unsafe_allow_html=True)


def cits(report: dict[str, Any], limit: int | None = None) -> None:
    rows = citations(report)
    st.markdown(f"<div class='ph'>Citations ({len(rows)})</div>", unsafe_allow_html=True)
    out = []
    for c in rows[:limit] if limit else rows:
        title = e(str(c["title"]))
        url = str(c.get("url") or "")
        name = f"<a href='{e(url)}' target='_blank' style='color:#c3cdea'>{title}</a>" if url.startswith("http") else title
        where = e(url.split('/')[2]) if url.startswith("http") else e(f"{c['kind']}{' p.' + str(c['page']) if c.get('page') else ''}")
        out.append(f"<div class='row'><span class='cit'>[{e(str(c['number']))}]</span><div class='tx'>"
                   f"<div class='tt' style='font-weight:500' title='{title}'>{name}</div><div class='ts'>{where}</div></div></div>")
    st.markdown("".join(out) or "<div class='muted'>No numbered citations in this report.</div>", unsafe_allow_html=True)


def _chart_frame(chart: dict[str, Any]) -> dict[str, list[Any]]:
    return {"label": chart["labels"], "value": chart["values"]}


def report_view(run_id: str) -> None:
    run = fetch(run_id)
    st.markdown(f"<a class='btn g' target='_self' href='{link('researches')}'>← My Researches</a>", unsafe_allow_html=True)
    if not run:
        st.error("Run not found.")
        return
    report = report_of(run)
    head("Research Report", title_of(run))
    if not report:
        st.info("This run has no report yet.")
        st.markdown(f"<a class='btn' target='_self' href='{link('researches', run=run_id, view='live')}'>Open live run →</a>",
                    unsafe_allow_html=True)
        return
    s = run_stats(run)
    tiles([("📌", "c2", "Status", s["status"], "run outcome", False),
           ("🔗", "c1", "Citations", str(len(citations(report))), "numbered sources", False),
           ("🧾", "c3", "Evidence", s["evidence"], "items gathered", False),
           ("⏱️", "c4", "Elapsed", fmt_duration(duration_seconds(run)), f"{s['calls']} calls", False)])
    left, right = st.columns([2, 1], gap="medium")
    with left, st.container(border=True, key="report"):
        charts = report_charts(report)
        body = str(report.get("content", ""))
        st.markdown(md_safe(strip_section(body, "Charts") if charts else body))
        for chart in charts:
            st.markdown(f"#### {e(chart['title'])}")
            st.bar_chart(_chart_frame(chart), x="label", y="value", horizontal=True, height=60 + 28 * len(chart["values"]))
        ver = verification_rows(report)
        if ver["rows"]:
            st.markdown(f"#### Fact check · support {e(ver['score'])} · {e(', '.join(ver['methods']))}")
            st.markdown("".join(f"<div class='st-row'><div style='min-width:0;flex:1'>{e(r['claim'])} "
                                f"<span class='muted'>[{e(r['cites'])}]</span></div>"
                                f"<span class='badge {r['css']}' title='{e(r['method'])}'>{e(r['label'])}</span></div>"
                                for r in ver["rows"][:25]), unsafe_allow_html=True)
        cl = claims(report)
        if cl:
            st.markdown("#### Claims")
            st.markdown("".join(f"<div class='st-row'><div style='min-width:0;flex:1'>{e(c['text'])}</div>"
                                f"<span class='badge b-ok'>{e(c['confidence'])}</span>"
                                f"<span class='muted' style='white-space:nowrap'>{c['evidence']} evidence</span></div>"
                                for c in cl[:12]), unsafe_allow_html=True)
    with right, st.container(border=True):
        cits(report)
        st.download_button("⬇ Download Markdown", str(report.get("content", "")), file_name=f"report-{run_id[:8]}.md",
                           use_container_width=True)


# ----- My Researches ---------------------------------------------------------------------------------
def page_researches() -> None:
    head("My Researches", "Every research run, live from the control API and saved to dashboard_runs/.")
    a, b = st.columns([3, 1])
    query = a.text_input("Search", placeholder="🔍  Search researches…", label_visibility="collapsed")
    opts = ["All", "Completed", "In Progress", "Needs approval", "Failed", "Stopped"]
    want = Q.get("status", "All")
    status = b.selectbox("Status", opts, index=opts.index(want) if want in opts else 0, label_visibility="collapsed")
    rows = filter_runs(runs_cache, query, status)
    body = "".join(run_row(r, tags=True) for r in rows[:100])
    st.markdown(f"<div class='panel'><div class='ph'>{len(rows)} of {len(runs_cache)} researches</div>"
                f"{body or '<div class=muted>No researches match.</div>'}</div>", unsafe_allow_html=True)


# ----- Knowledge Base ------------------------------------------------------------------------------
def page_knowledge() -> None:
    head("Knowledge Base", "Everything your researches produced: reports, claims, cited sources and uploaded files.")
    kb = knowledge(runs_cache)
    cats = [("📝", "c1", "Research Reports", len(kb["reports"])), ("💡", "c2", "Claims", len(kb["claims"])),
            ("🌐", "c5", "Web Sources", kb["web"]), ("📄", "c3", "Documents", kb["documents"]),
            ("🖼️", "c4", "Images", kb["images"]), ("🎬", "c6", "Videos", None)]
    cells = "".join(f"<div class='cap{' dis' if n is None else ''}'><div class='sq {c}' style='width:34px;height:34px'>{i}</div>"
                    f"<div class='ct'>{e(t)}</div><div class='cs'>{'coming soon' if n is None else f'{n} items'}</div></div>"
                    for i, c, t, n in cats)
    st.markdown(f"<div class='panel'><div class='ph'>Categories</div><div class='grid g6'>{cells}</div></div>", unsafe_allow_html=True)
    st.markdown(f"<div class='panel'><div class='ph'>Fact check</div><div class='muted'>{e(verification_line(kb['verification']))}"
                "</div></div>", unsafe_allow_html=True)
    t1, t2, t3 = st.tabs(["Reports", "Claims", "Files"])
    with t1:
        st.markdown("<div class='panel'>" + ("".join(
            f"<a class='row' target='_self' href='{link('researches', run=str(r['run_id']), view='report')}'><div class='sq c1'>📝</div>"
            f"<div class='tx'><div class='tt'>{e(r['title'])}</div><div class='ts'>{r['claims']} claims · {r['citations']} citations"
            + (f" · fact check {r['verification']['score']:.0%} {e(r['verification']['label'])}"
               if r.get('verification') and r['verification']['score'] is not None else "")
            + "</div></div>"
            f"<div class='rt'>{e(local_time(r['when']))}</div></a>" for r in kb["reports"])
            or "<div class='muted'>No reports yet.</div>") + "</div>", unsafe_allow_html=True)
    with t2:
        st.markdown("<div class='panel'>" + ("".join(
            f"<div class='row'><div class='sq c2'>💡</div><div class='tx'><div class='ts' style='white-space:normal;color:#e6ecff;font-size:.84rem'>"
            f"{e(c['text'])}</div></div><div class='rt'><span class='badge b-ok'>{e(c['confidence'])}</span></div></div>"
            for c in kb["claims"][:60]) or "<div class='muted'>No claims yet.</div>") + "</div>", unsafe_allow_html=True)
    with t3:
        st.markdown("<div class='panel'>" + ("".join(
            f"<div class='row'><div class='sq {'c3' if f['kind'] == 'pdf' else 'c4'}'>{'📄' if f['kind'] == 'pdf' else '🖼️'}</div>"
            f"<div class='tx'><div class='tt'>{e(f['name'])}</div><div class='ts'>run {e(str(f['run_id'])[:8])}</div></div>"
            f"<div class='rt'>{e(local_time(f['when']))}</div></div>" for f in kb["files"])
            or "<div class='muted'>No uploaded files yet. Attach a PDF or image on New Research.</div>") + "</div>",
            unsafe_allow_html=True)


# ----- Sources ---------------------------------------------------------------------------------------
def page_sources() -> None:
    head("Sources", "Where your research evidence comes from, and every source cited in a report.")
    s = Settings()
    rows = source_rows(runs_cache)
    providers = [("🌐", "c1", "Web Search", "Tavily / DuckDuckGo / Wikipedia", "Active" if s.research_mode == "live" else "Live mode"),
                 ("📄", "c3", "Documents", "Local PDF upload", "Active"), ("🖼️", "c4", "Images", "Local image upload", "Active"),
                 ("🔗", "c5", "Direct links", "PDF / image URL", "Active"), ("🎬", "c6", "Video", "YouTube etc.", "Coming soon"),
                 ("🗄️", "c2", "Databases", "Academic DBs", "Coming soon")]
    cells = "".join(f"<div class='cap{' dis' if st_ == 'Coming soon' else ''}'><div class='sq {c}' style='width:34px;height:34px'>{i}</div>"
                    f"<div class='ct'>{e(n)}</div><div class='cs'>{e(d)}<br><span class='badge {'b-soon' if st_ == 'Coming soon' else 'b-ok' if st_ == 'Active' else 'b-off'}'>"
                    f"{e(st_)}</span></div></div>" for i, c, n, d, st_ in providers)
    st.markdown(f"<div class='panel'><div class='ph'>Source types</div><div class='grid g6'>{cells}</div></div>", unsafe_allow_html=True)
    left, right = st.columns([1.6, 1], gap="medium")
    with left:
        kinds = ["All Sources", "Web", "Documents", "Images"]
        k = st.radio("Type", kinds, horizontal=True, label_visibility="collapsed")
        show = [r for r in rows if k == "All Sources" or source_kind(r) == k]
        body = "".join(f"<div class='row'><div class='sq {'c1' if source_kind(r) == 'Web' else 'c3'}'>{'🌐' if source_kind(r) == 'Web' else '📄'}</div>"
                       f"<div class='tx'><div class='tt' title='{e(str(r['title']))}'>"
                       + (f"<a href='{e(str(r['url']))}' target='_blank' style='color:#e6ecff'>{e(str(r['title']))}</a>" if r.get('url') else e(str(r['title'])))
                       + f"</div><div class='ts'>{e(r['domain'])}</div></div><div class='rt'>used in {len(r['runs'])} run(s)"
                       + (f"<br><span title='claims citing this source: supported / partial / unsupported / contradicted'>"
                          f"{e(verdict_label(r.get('verdicts')))}</span>" if verdict_label(r.get('verdicts')) else "")
                       + "</div></div>"
                       for r in show[:80])
        st.markdown(f"<div class='panel'><div class='ph'>Fact check</div><div class='muted'>"
                    f"{e(verification_line(knowledge(runs_cache)['verification']))}</div></div>", unsafe_allow_html=True)
        st.markdown(f"<div class='panel'><div class='ph'>Cited sources ({len(show)})</div>"
                    f"{body or '<div class=muted>No cited sources yet. Live runs cite real web pages.</div>'}</div>", unsafe_allow_html=True)
    with right:
        kv = [("Default search engine", {"auto": "Auto (Tavily → DuckDuckGo)", "duckduckgo": "DuckDuckGo", "tavily": "Tavily"}[s.search_provider]),
              ("Search results per task", str(s.live_max_search_results)), ("Pages fetched per task", str(s.live_max_fetch_pages)),
              ("File upload limit", "20 MB · up to 5 files"), ("Supported file types", "PDF, PNG, JPG, WEBP, GIF")]
        st.markdown("<div class='panel'><div class='ph'>Source Settings</div>" + "".join(
            f"<div class='kv'><span>{e(a)}</span><span title='{e(b)}'>{e(b)}</span></div>" for a, b in kv)
            + "<div class='muted' style='margin-top:8px'>Change these in .env (DEEP_RESEARCH_*).</div></div>", unsafe_allow_html=True)


# ----- Tools & Integration -------------------------------------------------------------------------
def page_tools() -> None:
    head("Tools & Integration", "The engine's real tools and providers. Switches show live state; they change with mode and keys.")
    keys = keys_or_none()
    mode = prefs.load()["default_mode"]
    left, right = st.columns([1.4, 1], gap="medium")
    with left, st.container(border=True):
        st.markdown(f"<div class='ph'>Available Tools <span class='muted'>default mode: {e(mode)}</span></div>", unsafe_allow_html=True)
        for t in tool_catalog(Settings(), keys, mode):
            a, b, c = st.columns([0.5, 4, 1.4], vertical_alignment="center")
            a.markdown(f"<div class='sq c1' style='width:36px;height:36px'>{t['icon']}</div>", unsafe_allow_html=True)
            b.markdown(f"<div style='min-width:0'><b>{e(t['name'])}</b><div class='muted ellipsis' title='{e(t['detail'])}'>"
                       f"{e(t['detail'])}</div></div>", unsafe_allow_html=True)
            c.toggle(t["state"].title(), value=t["state"] == "on", disabled=True, key=f"tool{t['name']}")
        st.markdown("<div class='muted'>Web tools switch on with live mode (Settings → Research). "
                    "Coming-soon tools are not faked.</div>", unsafe_allow_html=True)
    with right, st.container(border=True):
        st.markdown("<div class='ph'>Provider Health</div>", unsafe_allow_html=True)
        rows = provider_health(Settings(), keys, ok)
        st.markdown("".join(
            f"<div class='row'><div class='tx'><div class='tt'>{e(r['name'])}</div><div class='ts' title='{e(r['detail'])}'>{e(r['detail'])}</div></div>"
            f"<div class='rt'><span class='badge {'b-ok' if r['state'] == 'ok' else 'b-off' if r['state'] == 'off' else 'b-bad'}'>"
            f"{e(r['state'])}</span></div></div>" for r in rows), unsafe_allow_html=True)


# ----- Settings ------------------------------------------------------------------------------------------
def page_settings() -> None:
    head("Settings", "Customize defaults, research policy and API keys. Keys are write-only and never shown.")
    tabs = ["general", "research", "notifications", "account"]
    t = st.tabs(["General", "Research", "Notifications", "Account"])
    p = prefs.load()
    s = Settings()
    with t[0], st.container(border=True):
        st.markdown("<div class='ph'>General Settings</div>", unsafe_allow_html=True)
        a, b = st.columns(2)
        a.selectbox("Theme", ["Dark (navy)"], disabled=True, help="Only the dark theme is available.")
        b.selectbox("Time zone", [f"{zone} (system)"], disabled=True)
        mode = a.selectbox("Default research mode", ["offline", "live"], index=["offline", "live"].index(p["default_mode"]),
                           format_func=lambda m: "Live (free web + models)" if m == "live" else "Offline demo (no network)")
        lim = b.number_input("Items in Recent Researches", 3, 15, int(p["list_limit"]))
        step = st.toggle("Step-by-step approval on by default", value=bool(p["step_approval"]))
        if st.button("Save Changes", type="primary", key="savegen"):
            act(lambda: prefs.save(default_mode=mode, list_limit=int(lim), step_approval=step))
    with t[1]:
        a, b = st.columns(2, gap="medium")
        with a, st.container(border=True):
            st.markdown("<div class='ph'>Approval policy</div>", unsafe_allow_html=True)
            if ok:
                cur = client().settings()
                am = st.toggle("Step-by-step approval (approval mode)", value=cur.get("approval_mode", False))
                lo = st.toggle("Auto-approve low-risk steps", value=cur.get("auto_approve_low_risk", False))
                st.markdown("<div class='note'>Low-risk steps are planning, research, analysis and reporting. "
                            "High-risk or unknown steps always wait for a human.</div>", unsafe_allow_html=True)
                if st.button("Save policy", type="primary"):
                    act(lambda: client().update_settings(approval_mode=am, auto_approve_low_risk=lo))
            else:
                st.warning("Control API is offline. Start it with scripts/start-dashboard.ps1.")
        with b, st.container(border=True):
            st.markdown("<div class='ph'>Engine limits</div>", unsafe_allow_html=True)
            kv = [("Search provider", s.search_provider), ("Agent mode", s.agent_mode), ("Text model", s.openrouter_model),
                  ("Vision model", s.openrouter_vision_model), ("Max iterations", str(s.max_research_iterations)),
                  ("Tool / model calls", f"{s.max_tool_calls_total} / {s.max_model_calls_total}"),
                  ("Time limit", f"{int(s.max_research_time_seconds)} s")]
            st.markdown("".join(f"<div class='kv'><span>{e(x)}</span><span title='{e(y)}'>{e(y)}</span></div>" for x, y in kv)
                        + "<div class='muted' style='margin-top:8px'>Change these in .env (DEEP_RESEARCH_*).</div>", unsafe_allow_html=True)
    with t[2], st.container(border=True):
        st.markdown("<div class='ph'>Notifications <span class='badge b-soon'>coming soon</span></div>", unsafe_allow_html=True)
        st.toggle("Email me when a research completes", value=False, disabled=True)
        st.toggle("Desktop alert when a step needs approval", value=False, disabled=True)
        st.markdown("<div class='muted'>Not available yet. Runs waiting for approval are counted on the bell icon in the header.</div>",
                    unsafe_allow_html=True)
    with t[3], st.container(border=True):
        st.markdown("<div class='ph'>API keys</div>", unsafe_allow_html=True)
        if not ok:
            st.warning("Control API is offline.")
            return
        keys = client().keys().get("keys", {})
        st.markdown("".join(
            f"<div class='kv'><span>{e(n)}</span><span><span class='badge {'b-ok' if i.get('configured') else 'b-off'}'>"
            f"{e(i.get('masked') or 'not set')}</span></span></div>" for n, i in keys.items()), unsafe_allow_html=True)
        st.write("")
        a, b = st.columns([1, 2])
        name = a.selectbox("Key to update", list(keys))
        value = b.text_input("New value", type="password", placeholder="paste the key, it is never shown again")
        if st.button("Save key", type="primary"):
            act(lambda: client().set_key(name, value))
        st.markdown("<div class='muted'>Saved to the local key store (file mode 0600). New keys apply on the next run.</div>",
                    unsafe_allow_html=True)
    _ = tabs


view, rid = Q.get("view"), Q.get("run")
if view in ("live", "report") and rid:
    (live_view if view == "live" else report_view)(rid)
else:
    {"dashboard": page_dashboard, "new": page_new, "researches": page_researches, "knowledge": page_knowledge,
     "sources": page_sources, "tools": page_tools, "settings": page_settings}[page]()
