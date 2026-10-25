"""Dashboard wiring for Phase 5: report chat/notes/share, profile settings, reminders (offline AppTest)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import warnings

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")  # starlette httpx deprecation notice
    from fastapi.testclient import TestClient

from deep_research.assistant.notes import NotesService
from deep_research.assistant.profile import UserProfile, load_profile, save_profile
from deep_research.assistant.store import AssistantStore
from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.dashboard.core import ControlClient, RunStore

ROOT = Path(__file__).resolve().parents[2]
APP = str(ROOT / "src/deep_research/dashboard/app.py")
TOKEN = "k" * 32
REPORT = {"title": "Tea", "content": "# Tea\n\n## Findings\nGreen tea contains catechins and caffeine [1].\n",
          "citations": [{"number": 1, "title": "Tea study", "url": "https://tea.example/a"}]}


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AssistantStore:
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("DEEP_RESEARCH_ASSISTANT_DB", str(tmp_path / "data" / "assistant.db"))
    RunStore(tmp_path / "runs").save({"run_id": "tea1", "status": "completed", "created_at": "2026-10-24T10:00:00+00:00",
                                      "params": {"objective": "Tea health", "mode": "offline"},
                                      "result": {"report": REPORT}})
    return AssistantStore(tmp_path / "data" / "assistant.db")


def _text(at: Any) -> str:
    return " ".join(m.value for m in at.markdown) + " ".join(c.value for c in at.caption)


def test_report_view_chat_is_grounded_and_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    store = _setup(tmp_path, monkeypatch)
    NotesService(store).add_note("tea1", "check the dose")
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params.update({"page": "researches", "run": "tea1", "view": "report"})
    at.run()
    assert not at.exception
    assert [t.label for t in at.tabs][-4:] == ["💬 Ask this report", "📝 Notes & reminders", "🔗 Share", "⏱ Trace"]
    assert any("check the dose" in x.label for x in at.expander)
    at.chat_input[0].set_value("Does green tea contain catechins?").run()
    assert not at.exception
    hist = store.chat_history("tea1")
    assert [h["role"] for h in hist] == ["user", "assistant"] and "[1]" in hist[1]["text"]
    at.chat_input[0].set_value("What is the capital of Peru?").run()
    assert store.chat_history("tea1")[-1]["meta"]["supported"] is False
    assert "not supported by this report" in _text(at)
    assert "Control API is offline" in " ".join(w.value for w in at.warning)


def test_dashboard_shows_upcoming_reminders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    svc = NotesService(_setup(tmp_path, monkeypatch))
    svc.add_reminder("re-read tea report", datetime.now(timezone.utc) + timedelta(days=2), run_id="tea1")
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = "dashboard"
    at.run()
    assert not at.exception
    text = _text(at)
    assert "Upcoming Reminders" in text and "re-read tea report" in text


def test_settings_profile_and_reminders_tabs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    store = _setup(tmp_path, monkeypatch)
    save_profile(store, UserProfile(saved_topics=["green tea"], default_depth="deep"))
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = "settings"
    at.run()
    assert not at.exception
    labels = [t.label for t in at.tabs]
    assert "Profile & Preferences" in labels and "Reminders" in labels and "Notifications" not in labels
    assert "Notifications coming soon" not in _text(at)
    area = next(a for a in at.text_area if a.label.startswith("Saved topics"))
    assert area.value == "green tea"
    area.set_value("green tea\nmatcha")
    next(b for b in at.button if b.label == "Save profile").click().run()
    assert not at.exception
    assert load_profile(store).saved_topics == ["green tea", "matcha"] and load_profile(store).default_depth == "deep"


def test_client_share_methods(tmp_path: Path) -> None:
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path / "cfg",
                                  assistant_db=tmp_path / "a.db")
    tc = TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t",
                                      assistant=AssistantStore(tmp_path / "a.db")))

    def handler(request: httpx.Request) -> httpx.Response:
        resp = tc.request(request.method, request.url.path, params=request.url.params,
                          headers=dict(request.headers), content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    api = ControlClient("http://test", TOKEN, transport=httpx.MockTransport(handler))
    share = api.create_share("tea1", REPORT)
    assert share["url"].startswith("http://test/share/") and "token=" in share["url"]
    assert tc.get(share["url"].removeprefix("http://test")).status_code == 200
    assert [s["id"] for s in api.list_shares("tea1")] == [share["id"]]
    assert api.revoke_share(share["id"])["active"] is False
    assert tc.get(share["url"].removeprefix("http://test")).status_code == 404


def test_reminders_manage_link_opens_settings_reminders_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    import streamlit as st

    from deep_research.dashboard.app import settings_default_tab

    assert settings_default_tab("reminders") == "Reminders" and settings_default_tab(" Profile ") == "Profile & Preferences"
    assert settings_default_tab(None) is None and settings_default_tab("bogus") is None
    _setup(tmp_path, monkeypatch)
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.run()
    assert "page=settings&amp;tab=reminders" in _text(at) or "page=settings&tab=reminders" in _text(at)
    seen: list[tuple[list[str], Any]] = []
    real = st.tabs

    def spy(labels: Any, **kw: Any) -> Any:
        seen.append((list(labels), kw.get("default")))
        return real(labels, **kw)

    monkeypatch.setattr(st, "tabs", spy)
    for tab, expected in (("reminders", "Reminders"), (None, None)):
        seen.clear()
        at = testing.AppTest.from_file(APP, default_timeout=30)
        at.query_params["page"] = "settings"
        if tab:
            at.query_params["tab"] = tab
        at.run()
        assert not at.exception
        settings_call = next(d for labels, d in seen if "Reminders" in labels and "Account" in labels)
        assert settings_call == expected
