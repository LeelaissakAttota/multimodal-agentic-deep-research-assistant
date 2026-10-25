"""Static HTML export + revocable token-protected share links on the control API (no network)."""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.assistant.share import ShareService, render_html
from deep_research.assistant.store import AssistantStore
from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.export import export_report

TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
REPORT: dict[str, Any] = {
    "title": "Tea <script>alert(1)</script>",
    "content": "# Tea\n\n## Findings\nGreen tea has catechins [1].\n- bullet <b>x</b> [2]\n\n"
               "| a | b |\n|---|---|\n| 1 | 2 |\n\n## Sources\n1. A - https://a.example\n",
    "citations": [{"number": 1, "title": "A", "url": "https://a.example/x"},
                  {"number": 2, "title": "B", "url": "javascript:alert(1)"}],
    "claim_scores": [{"text": "Green tea has catechins.", "citations": [1], "confidence": 0.8, "label": "high"}],
}


def test_html_is_self_contained_and_escaped() -> None:
    html = render_html(REPORT)
    assert "<script" not in html.lower() and "&lt;script&gt;" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", html.split("<h2>Sources</h2>")[0])
    assert "<style>" in html and "<link" not in html
    assert 'href="#src-1"' in html and 'id="src-1"' in html
    assert 'href="https://a.example/x"' in html
    assert 'href="javascript' not in html
    assert "<table>" in html and "Claim confidence" in html


def test_export_html_format() -> None:
    assert export_report(REPORT, "html").startswith(b"<!doctype html>")


def test_share_service_token_and_revoke(tmp_path: Path) -> None:
    svc = ShareService(AssistantStore(tmp_path / "a.db"))
    info, token = svc.create("run1", REPORT)
    assert info["active"] and len(token) >= 40
    assert svc.open(info["id"], token) is not None
    assert svc.open(info["id"], token + "x") is None
    assert svc.open(info["id"], "") is None
    assert svc.open("unknown-id-1", token) is None
    with svc.store.connect() as db:
        stored = db.execute("SELECT * FROM shares").fetchone()
    assert token not in str(dict(stored))
    svc.revoke(info["id"])
    assert svc.open(info["id"], token) is None and svc.list("run1")[0]["active"] is False
    with pytest.raises(KeyError):
        svc.revoke("nope")
    with pytest.raises(ValueError):
        svc.create("run1", {"title": "empty"})


@pytest.fixture()
def api(tmp_path: Path) -> tuple[TestClient, DeepResearchControl]:
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path / "cfg",
                                  assistant_db=tmp_path / "data" / "assistant.db")
    app = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="deep-research",
                            assistant=AssistantStore(control.assistant_db))
    return TestClient(app), control


def test_share_api_flow(api: tuple[TestClient, DeepResearchControl]) -> None:
    client, control = api
    assert client.post("/control/runs/r1/shares", json={"report": REPORT}).status_code == 401
    assert client.post("/control/runs/r1/shares", json={}, headers=H).status_code == 409
    resp = client.post("/control/runs/r1/shares", json={"report": REPORT}, headers=H)
    assert resp.status_code == 201
    share = resp.json()
    path = share["path"]
    page = client.get(path)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert "Green tea has catechins" in page.text
    assert client.get(f"/share/{share['id']}?token=wrong").status_code == 404
    assert client.get(f"/share/{share['id']}").status_code == 404
    listed = client.get("/control/shares", headers=H).json()
    assert [s["id"] for s in listed] == [share["id"]] and "token" not in str(listed).lower()
    assert client.post(f"/control/shares/{share['id']}/revoke").status_code == 401
    assert client.post(f"/control/shares/{share['id']}/revoke", headers=H).json()["active"] is False
    assert client.get(path).status_code == 404
    assert client.post("/control/shares/missing/revoke", headers=H).status_code == 404
    audit = str(control.manager.audit.tail(20))
    assert "share_created" in audit and "share_revoked" in audit
    token = path.split("token=")[1]
    assert token not in audit


def test_share_api_rejects_bad_run_id(api: tuple[TestClient, DeepResearchControl]) -> None:
    client, _ = api
    assert client.post("/control/runs/bad.id/shares", json={"report": REPORT}, headers=H).status_code == 422


def test_share_routes_absent_without_store(tmp_path: Path) -> None:
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path)
    client = TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t"))
    assert client.get("/control/shares", headers=H).status_code == 404
