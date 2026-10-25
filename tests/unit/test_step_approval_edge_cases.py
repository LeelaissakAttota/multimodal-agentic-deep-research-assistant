"""Edge cases for "Step-by-step approval" (Testing Bot): precedence, validation, corrupt files, flows, queue routing."""

from __future__ import annotations

import json
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.runtime import APPROVAL_ENV, ControlSettings
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.dashboard.core import approval_state_html

TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture(autouse=True)
def _no_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APPROVAL_ENV, raising=False)
    monkeypatch.delenv("MDRA_REDIS_URL", raising=False)
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(tmp_path / "missing.env"))


def _control(cfg: Path, **kw: Any) -> DeepResearchControl:
    kw.setdefault("with_queue", False)
    return DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=cfg,
                               assistant_db=cfg / "a.db", **kw)


def _client(control: DeepResearchControl) -> TestClient:
    return TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t"))


def _wait(c: TestClient, rid: str, pred: Any, timeout: float = 15) -> dict[str, Any]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        run = c.get(f"/control/runs/{rid}", headers=H).json()
        if pred(run):
            return dict(run)
        time.sleep(0.03)
    raise AssertionError(f"timeout: {run['status']}")


def _done(r: dict[str, Any]) -> bool:
    return r["status"] in ("completed", "failed", "stopped")


def _pending(r: dict[str, Any]) -> bool:
    return bool(r["pending_approvals"]) or _done(r)


def _pending_id(c: TestClient, rid: str) -> str:
    steps = c.get(f"/control/runs/{rid}/steps", headers=H).json()
    return next(s["step_id"] for s in steps if s["state"] == "pending_approval")


def _create(c: TestClient, **extra: Any) -> dict[str, Any]:
    r = c.post("/control/runs", json={"params": {"objective": "tea"}, **extra}, headers=H)
    assert r.status_code == 201, r.text
    return dict(r.json())


# ---- precedence matrix -------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("env", "saved", "per_run", "mode", "source"),
    [
        (None, None, None, False, "settings"),
        ("true", None, None, True, "settings"),
        ("false", None, None, False, "settings"),
        ("true", False, None, False, "settings"),
        (None, True, None, True, "settings"),
        ("false", True, None, True, "settings"),
        ("true", True, False, False, "run"),
        (None, True, False, False, "run"),
        (None, False, True, True, "run"),
        ("true", None, False, False, "run"),
        ("false", None, True, True, "run"),
    ],
)
def test_precedence_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env: str | None, saved: bool | None,
                           per_run: bool | None, mode: bool, source: str) -> None:
    if env is not None:
        monkeypatch.setenv(APPROVAL_ENV, env)
    control = _control(tmp_path)
    c = _client(control)
    if saved is not None:
        assert c.put("/control/settings", json={"approval_mode": saved}, headers=H).status_code == 200
    before = (tmp_path / "control_settings.json").read_text() if saved is not None else None
    run = _create(c, **({} if per_run is None else {"approval_mode": per_run}))
    assert run["approval_mode"] is mode and run["approval_source"] == source
    # per-run override never persisted
    if saved is None:
        assert not (tmp_path / "control_settings.json").exists()
    else:
        assert (tmp_path / "control_settings.json").read_text() == before
    control.manager.get(run["run_id"]).stop()


@pytest.mark.parametrize("value", ["1", "TRUE", "yes", "On", "y"])
def test_env_truthy_coercion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(APPROVAL_ENV, value)
    assert ControlSettings(tmp_path / "s.json").load()["approval_mode"] is True


@pytest.mark.parametrize("value", ["", "0", "no", "off", "maybe", "2", "enabled"])
def test_env_other_values_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(APPROVAL_ENV, value)
    assert ControlSettings(tmp_path / "s.json").load()["approval_mode"] is False


def test_saving_other_key_freezes_env_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Documented: once the file is written (any key), the env value is snapshotted into it."""
    monkeypatch.setenv(APPROVAL_ENV, "true")
    store = ControlSettings(tmp_path / "s.json")
    store.update(auto_approve_low_risk=True)
    monkeypatch.setenv(APPROVAL_ENV, "false")
    assert store.load()["approval_mode"] is True


# ---- invalid values ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["maybe", 2, -1, [], {}, "2"])
@pytest.mark.parametrize("endpoint", ["run", "settings"])
def test_invalid_values_rejected_422(tmp_path: Path, value: Any, endpoint: str) -> None:
    c = _client(_control(tmp_path))
    if endpoint == "run":
        r = c.post("/control/runs", json={"params": {"objective": "x"}, "approval_mode": value}, headers=H)
    else:
        r = c.put("/control/settings", json={"approval_mode": value}, headers=H)
    assert r.status_code == 422
    assert not (tmp_path / "control_settings.json").exists()
    assert c.get("/control/runs", headers=H).json() == []


@pytest.mark.parametrize(("value", "expected"), [("yes", True), ("ON", True), ("1", True), (1, True),
                                                 ("off", False), (0, False), ("false", False)])
def test_lax_bool_coercion_documented(tmp_path: Path, value: Any, expected: bool) -> None:
    """Pydantic lax bool coercion is accepted for both the per-run field and the saved setting."""
    control = _control(tmp_path)
    c = _client(control)
    run = _create(c, approval_mode=value)
    assert run["approval_mode"] is expected and run["approval_source"] == "run"
    control.manager.get(run["run_id"]).stop()
    assert c.put("/control/settings", json={"approval_mode": value}, headers=H).json()["approval_mode"] is expected


def test_null_per_run_means_use_settings(tmp_path: Path) -> None:
    control = _control(tmp_path)
    c = _client(control)
    c.put("/control/settings", json={"approval_mode": True}, headers=H)
    run = _create(c, approval_mode=None)
    assert run["approval_mode"] is True and run["approval_source"] == "settings"
    control.manager.get(run["run_id"]).stop()


def test_null_setting_leaves_saved_value(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    c.put("/control/settings", json={"approval_mode": True}, headers=H)
    assert c.put("/control/settings", json={"approval_mode": None}, headers=H).json()["approval_mode"] is True


# ---- corrupt control_settings.json ------------------------------------------------------------------------

@pytest.mark.parametrize("content", ["{not json", "", "{}"])
def test_corrupt_settings_file_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    path = tmp_path / "control_settings.json"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv(APPROVAL_ENV, "true")
    try:
        loaded = ControlSettings(path).load()
    except Exception as exc:  # pragma: no cover - reported as bug below for some inputs
        pytest.fail(f"load crashed: {type(exc).__name__}")
    assert loaded["approval_mode"] is True


@pytest.mark.xfail(strict=True, reason="BUG: non-dict JSON (42/true/null/[]/string) in control_settings.json crashes "
                                       "load() with TypeError/AttributeError -> 500 on GET /control/settings and run create")
@pytest.mark.parametrize("content", ["42", "true", "null", "[]", '"approval_mode"'])
def test_corrupt_settings_scalar_json(tmp_path: Path, content: str) -> None:
    path = tmp_path / "control_settings.json"
    path.write_text(content, encoding="utf-8")
    assert ControlSettings(path).load()["approval_mode"] is False


@pytest.mark.xfail(strict=True, reason="BUG: hand-edited string \"false\" in control_settings.json is bool()-coerced "
                                       "to True (step approval silently ON)")
def test_settings_file_string_false(tmp_path: Path) -> None:
    path = tmp_path / "control_settings.json"
    path.write_text(json.dumps({"approval_mode": "false"}), encoding="utf-8")
    assert ControlSettings(path).load()["approval_mode"] is False


def test_saving_repairs_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "control_settings.json"
    path.write_text("{oops", encoding="utf-8")
    c = _client(_control(tmp_path))
    assert c.put("/control/settings", json={"approval_mode": True}, headers=H).status_code == 200
    assert json.loads(path.read_text())["approval_mode"] is True


# ---- on-mode flows ----------------------------------------------------------------------------------------

def test_on_mode_walk_all_approvals(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    rid = _create(c, approval_mode=True)["run_id"]
    approved = 0
    for _ in range(50):
        run = _wait(c, rid, _pending)
        if _done(run):
            break
        assert run["status"] == "waiting_approval"
        sid = _pending_id(c, rid)
        assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 200
        approved += 1
    final = _wait(c, rid, _done)
    assert final["status"] == "completed" and approved >= 2
    manual = [s for s in final["steps"] if s["decision"] == "manual"]
    assert len(manual) == approved


def test_double_approve_and_approve_finished_409(tmp_path: Path) -> None:
    control = _control(tmp_path)
    c = _client(control)
    rid = _create(c, approval_mode=True)["run_id"]
    _wait(c, rid, lambda r: r["pending_approvals"])
    sid = _pending_id(c, rid)
    assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 200
    assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 409
    assert c.post(f"/control/runs/{rid}/steps/{sid}/reject", json={}, headers=H).status_code == 409
    c.post(f"/control/runs/{rid}/stop", headers=H)
    _wait(c, rid, _done)
    assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 409


def test_reject_stops_run(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    rid = _create(c, approval_mode=True)["run_id"]
    _wait(c, rid, lambda r: r["pending_approvals"])
    sid = _pending_id(c, rid)
    assert c.post(f"/control/runs/{rid}/steps/{sid}/reject", json={"note": "no"}, headers=H).status_code == 200
    final = _wait(c, rid, lambda r: _done(r) or not r["pending_approvals"] and r["status"] != "running")
    assert final["status"] != "completed" or all(s["decision"] != "manual" for s in final["steps"])
    assert any(s["decision"] == "rejected" for s in final["steps"])


def test_off_mode_has_nothing_to_approve(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    rid = _create(c, approval_mode=False)["run_id"]
    final = _wait(c, rid, _done)
    assert final["status"] == "completed"
    sid = final["steps"][0]["step_id"]
    assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 409


def test_unknown_ids_404(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    assert c.post("/control/runs/nope/steps/x/approve", json={}, headers=H).status_code == 404
    assert c.post("/control/runs/nope/resume", headers=H).status_code == 404
    rid = _create(c, approval_mode=True)["run_id"]
    _wait(c, rid, lambda r: r["pending_approvals"])
    assert c.post(f"/control/runs/{rid}/steps/zzz/approve", json={}, headers=H).status_code == 404
    c.post(f"/control/runs/{rid}/stop", headers=H)


def test_pause_resume_while_waiting(tmp_path: Path) -> None:
    c = _client(_control(tmp_path))
    rid = _create(c, approval_mode=True)["run_id"]
    _wait(c, rid, lambda r: r["pending_approvals"])
    p = c.post(f"/control/runs/{rid}/pause", headers=H)
    assert p.status_code in (200, 409)
    r = c.post(f"/control/runs/{rid}/resume", headers=H)
    assert r.status_code in (200, 409)
    sid = _pending_id(c, rid)
    assert c.post(f"/control/runs/{rid}/steps/{sid}/approve", json={}, headers=H).status_code == 200
    c.post(f"/control/runs/{rid}/stop", headers=H)
    assert _wait(c, rid, _done)["status"] == "stopped"


def test_concurrent_mixed_modes_no_leak(tmp_path: Path) -> None:
    control = _control(tmp_path)
    c = _client(control)
    c.put("/control/settings", json={"approval_mode": True}, headers=H)
    results: dict[int, dict[str, Any]] = {}

    def make(i: int) -> None:
        extra = {} if i % 3 == 0 else {"approval_mode": bool(i % 2)}
        results[i] = _create(c, **extra)

    threads = [threading.Thread(target=make, args=(i,)) for i in range(9)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for i, run in results.items():
        exp = True if i % 3 == 0 else bool(i % 2)
        assert run["approval_mode"] is exp
        assert run["approval_source"] == ("settings" if i % 3 == 0 else "run")
        final = _wait(c, run["run_id"], _pending)
        assert (final["status"] == "completed") is (not exp)
    assert c.get("/control/settings", headers=H).json()["approval_mode"] is True
    for run in results.values():
        control.manager.get(run["run_id"]).stop()


# ---- queue routing ----------------------------------------------------------------------------------------

class _Queue:
    name = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.submitted: list[str] = []

    def submit(self, payload: dict[str, Any]) -> str:
        if self.fail:
            raise ConnectionError("down")
        self.submitted.append(payload["run_id"])
        return "job-" + payload["run_id"]

    def status(self, job_id: str) -> dict[str, Any]:
        return {"status": "finished", "result": {"status": "completed"}}

    def cancel(self, job_id: str) -> None:
        pass


def test_off_enqueues_on_never_enqueues(tmp_path: Path) -> None:
    q = _Queue()
    control = _control(tmp_path, queue=q)
    c = _client(control)
    c.put("/control/settings", json={"approval_mode": True}, headers=H)
    off = _create(c, approval_mode=False)
    on_saved = _create(c)
    on_run = _create(c, approval_mode=True)
    _wait(c, off["run_id"], _done)
    _wait(c, on_saved["run_id"], lambda r: r["pending_approvals"])
    _wait(c, on_run["run_id"], lambda r: r["pending_approvals"])
    assert q.submitted == [off["run_id"]]
    for r in (on_saved, on_run):
        control.manager.get(r["run_id"]).stop()


def test_queue_failure_falls_back_in_process(tmp_path: Path) -> None:
    control = _control(tmp_path, queue=_Queue(fail=True))
    c = _client(control)
    run = _create(c, approval_mode=False)
    final = _wait(c, run["run_id"], _done)
    assert final["status"] == "completed"
    assert control.queue_fallback == "ConnectionError"


# ---- auth & dashboard -------------------------------------------------------------------------------------

@pytest.mark.parametrize(("method", "path"), [("get", "/control/settings"), ("put", "/control/settings"),
                                              ("post", "/control/runs"), ("post", "/control/runs/x/steps/y/approve"),
                                              ("post", "/control/runs/x/steps/y/reject"), ("post", "/control/runs/x/resume")])
@pytest.mark.parametrize("hdr", [{}, {"Authorization": "Bearer wrong-token-wrong-token"}, {"Authorization": TOKEN}])
def test_auth_required(tmp_path: Path, method: str, path: str, hdr: dict[str, str]) -> None:
    c = _client(_control(tmp_path))
    r = getattr(c, method)(path, headers=hdr, **({} if method == "get" else {"json": {"approval_mode": True}}))
    assert r.status_code == 401
    assert not (tmp_path / "control_settings.json").exists()


def test_run_page_line_edge_values() -> None:
    assert "ON" in approval_state_html({"approval_mode": True})
    html = approval_state_html({"approval_mode": False, "approval_source": "<script>"})
    assert "OFF" in html and "<script>" not in html
    assert "OFF" in approval_state_html({})


# ---- .env support (added in 4f59bf6) ----------------------------------------------------------------------

def test_dotenv_used_and_real_env_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "x.env"
    env.write_text("DEEP_RESEARCH_APPROVAL_MODE=true\n", encoding="utf-8")
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(env))
    store = ControlSettings(tmp_path / "s.json")
    assert store.load()["approval_mode"] is True
    monkeypatch.setenv(APPROVAL_ENV, "false")
    assert store.load()["approval_mode"] is False
    monkeypatch.delenv(APPROVAL_ENV)
    store.update(approval_mode=False)
    assert store.load()["approval_mode"] is False


def test_dotenv_garbage_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / "x.env"
    env.write_text("DEEP_RESEARCH_APPROVAL_MODE=sure\nOTHER=1\n", encoding="utf-8")
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(env))
    assert ControlSettings(tmp_path / "s.json").load()["approval_mode"] is False


def test_missing_env_file_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_RESEARCH_ENV_FILE", str(tmp_path / "nope" / "missing.env"))
    assert ControlSettings(tmp_path / "s.json").load()["approval_mode"] is False
