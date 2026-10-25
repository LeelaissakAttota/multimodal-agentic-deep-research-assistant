""""Step-by-step approval": saved setting (default OFF), env default, per-run override, queue routing, dashboard."""

from __future__ import annotations

import json
import time
import warnings
from pathlib import Path
from typing import Any

import httpx
import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.runtime import APPROVAL_ENV, ControlSettings, RunStatus
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.dashboard import core
from deep_research.dashboard.core import STEP_HELP, STEP_LABEL, ControlClient, approval_state_html
from deep_research.scale.queue import InProcessQueue

ROOT = Path(__file__).resolve().parents[2]
APP = str(ROOT / "src/deep_research/dashboard/app.py")
TOKEN = "s" * 32
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _no_env_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APPROVAL_ENV, raising=False)


def _control(cfg: Path, **kw: Any) -> DeepResearchControl:
    return DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=cfg,
                               assistant_db=cfg / "a.db", **kw)


def _client(control: DeepResearchControl) -> TestClient:
    return TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="deep-research"))


def _wait(client: TestClient, rid: str, pred: Any, timeout: float = 15) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        run = client.get(f"/control/runs/{rid}", headers=H).json()
        if pred(run):
            return dict(run)
        time.sleep(0.05)
    raise AssertionError("timeout")


def _done(r: dict[str, Any]) -> bool:
    return r["status"] in ("completed", "failed", "stopped")


def test_default_off_runs_straight_through(tmp_path: Path) -> None:
    client = _client(_control(tmp_path))
    assert client.get("/control/settings", headers=H).json()["approval_mode"] is False
    run = client.post("/control/runs", json={"params": {"objective": "tea"}}, headers=H).json()
    assert run["approval_mode"] is False and run["approval_source"] == "settings"
    final = _wait(client, run["run_id"], _done)
    assert final["status"] == "completed" and all(s["decision"] != "manual" for s in final["steps"])


def test_setting_persists_across_restart(tmp_path: Path) -> None:
    assert _client(_control(tmp_path)).put("/control/settings", json={"approval_mode": True}, headers=H).status_code == 200
    restarted = _client(_control(tmp_path))  # new process, same config dir
    assert restarted.get("/control/settings", headers=H).json()["approval_mode"] is True
    run = restarted.post("/control/runs", json={"params": {"objective": "tea"}}, headers=H).json()
    paused = _wait(restarted, run["run_id"], lambda r: r["pending_approvals"])
    assert paused["status"] == "waiting_approval" and paused["approval_source"] == "settings"
    restarted.post(f"/control/runs/{run['run_id']}/stop", headers=H)
    assert json.loads((tmp_path / "control_settings.json").read_text(encoding="utf-8"))["approval_mode"] is True


def test_env_flag_is_initial_default_until_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ControlSettings(tmp_path / "control_settings.json")
    monkeypatch.setenv(APPROVAL_ENV, "true")
    assert store.load()["approval_mode"] is True
    store.update(approval_mode=False)  # explicit choice in the dashboard wins from now on
    assert store.load()["approval_mode"] is False
    monkeypatch.setenv(APPROVAL_ENV, "0")
    assert ControlSettings(tmp_path / "other.json").load()["approval_mode"] is False


def test_per_run_override_both_ways(tmp_path: Path) -> None:
    client = _client(_control(tmp_path))
    on = client.post("/control/runs", json={"params": {"objective": "a"}, "approval_mode": True}, headers=H).json()
    assert on["approval_mode"] is True and on["approval_source"] == "run"
    assert _wait(client, on["run_id"], lambda r: r["pending_approvals"])["status"] == "waiting_approval"
    client.post(f"/control/runs/{on['run_id']}/stop", headers=H)
    client.put("/control/settings", json={"approval_mode": True}, headers=H)
    off = client.post("/control/runs", json={"params": {"objective": "b"}, "approval_mode": False}, headers=H).json()
    assert off["approval_mode"] is False and off["approval_source"] == "run"
    assert _wait(client, off["run_id"], _done)["status"] == "completed"
    assert client.get("/control/settings", headers=H).json()["approval_mode"] is True  # override is not saved
    bad = client.post("/control/runs", json={"params": {}, "approval_mode": "maybe"}, headers=H)
    assert bad.status_code == 422


def test_off_goes_to_queue_on_stays_in_process(tmp_path: Path) -> None:
    seen: list[str] = []

    def job(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload["run_id"])
        return {"status": "completed"}

    control = _control(tmp_path, queue=InProcessQueue(job))
    off = control.manager.create({"objective": "q"})
    on = control.manager.create({"objective": "q"}, approval_mode=True)
    end = time.monotonic() + 10
    while (off.status != RunStatus.COMPLETED or on.status != RunStatus.WAITING_APPROVAL) and time.monotonic() < end:
        time.sleep(0.05)
    assert seen == [off.run_id] and on.status == RunStatus.WAITING_APPROVAL
    on.stop()


def test_client_sends_override_only_when_given() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"run_id": "r"})

    api = ControlClient("http://test", TOKEN, transport=httpx.MockTransport(handler))
    api.create_run("x", mode="offline")
    api.create_run("x", mode="offline", approval_mode=True)
    assert "approval_mode" not in bodies[0] and bodies[1]["approval_mode"] is True


def test_run_page_state_line() -> None:
    assert f"{STEP_LABEL}: ON" in approval_state_html({"approval_mode": True, "approval_source": "run"})
    assert "set for this run" in approval_state_html({"approval_mode": True, "approval_source": "run"})
    off = approval_state_html({"approval_mode": False, "approval_source": "settings"})
    assert f"{STEP_LABEL}: OFF" in off and "from Settings" in off
    assert STEP_HELP == "Asks for your OK before each important step"


def _app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: DeepResearchControl, page: str) -> Any:
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("DEEP_RESEARCH_ASSISTANT_DB", str(tmp_path / "data" / "assistant.db"))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_TOKEN", TOKEN)
    test_client = _client(control)

    def handler(request: httpx.Request) -> httpx.Response:
        resp = test_client.request(request.method, request.url.path, params=request.url.params,
                                   headers=dict(request.headers), content=request.content)
        return httpx.Response(resp.status_code, content=resp.content, headers=resp.headers)

    real_init = core.ControlClient.__init__

    def init(self: ControlClient, *a: Any, **kw: Any) -> None:
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, "http://test", TOKEN, **{k: v for k, v in kw.items() if k not in ("base_url", "token")})

    monkeypatch.setattr(core.ControlClient, "__init__", init)
    import streamlit as st

    st.cache_data.clear()
    st.cache_resource.clear()
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = page
    if page == "settings":
        at.query_params["tab"] = "research"
    at.run()
    assert not at.exception, at.exception
    return at


def test_dashboard_settings_toggle_saves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = _control(tmp_path / "cfg")
    at = _app(tmp_path, monkeypatch, control, "settings")
    toggle = next(t for t in at.toggle if t.label == STEP_LABEL)
    assert toggle.value is False and toggle.help == STEP_HELP
    toggle.set_value(True)
    next(b for b in at.button if b.label == "Save policy").click().run()
    assert not at.exception
    assert control.manager.settings.load()["approval_mode"] is True


def test_dashboard_new_research_checkbox_defaults_to_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control = _control(tmp_path / "cfg")
    control.manager.settings.update(approval_mode=True)
    at = _app(tmp_path, monkeypatch, control, "new")
    box = next(c for c in at.checkbox if c.label == STEP_LABEL)
    assert box.value is True and STEP_HELP in (box.help or "")
