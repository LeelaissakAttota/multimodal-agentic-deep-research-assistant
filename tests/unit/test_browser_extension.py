"""Chrome MV3 extension: manifest/security checks, node unit tests, and the control API clip inbox."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.control.api import build_control_app
from deep_research.control.clips import MAX_CLIPS, ClipStore, clean_page_url
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "extensions" / "mdra-chrome"
TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
LOCAL = ("127.0.0.1", 50000)


def test_manifest_is_mv3_and_local_only() -> None:
    m = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3 and m["background"] == {"service_worker": "background.js", "type": "module"}
    assert m["host_permissions"] == ["http://127.0.0.1/*", "http://localhost/*"]
    assert set(m["permissions"]) == {"storage", "contextMenus", "activeTab", "scripting"}
    assert "<all_urls>" not in json.dumps(m) and "content_scripts" not in m
    for ref in (m["background"]["service_worker"], m["action"]["default_popup"], m["options_ui"]["page"]):
        assert (EXT / ref).is_file()
    for html in EXT.glob("*.html"):
        text = html.read_text(encoding="utf-8")
        assert "<script>" not in text and "onclick=" not in text  # MV3 CSP: no inline script
        for src in re.findall(r'src="([^"]+)"', text):
            assert (EXT / src).is_file()


def test_token_never_logged_or_synced() -> None:
    for js in EXT.glob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert "console." not in text, js.name
        assert "storage.sync" not in text and "localStorage" not in text, js.name
        assert "innerHTML" not in text, js.name
    options = (EXT / "options.js").read_text(encoding="utf-8")
    assert '$("token").value = ""' in options and "$(\"token\").value = s.token" not in options
    assert 'type="password"' in (EXT / "options.html").read_text(encoding="utf-8")


def test_node_unit_tests() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    proc = subprocess.run([node, "--test", str(ROOT / "tests" / "extension" / "api.test.mjs")], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]


def _client(tmp_path: Path, host: tuple[str, int] = LOCAL) -> tuple[TestClient, DeepResearchControl, ClipStore]:
    control = DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=tmp_path / "cfg",
                                  assistant_db=tmp_path / "a.db")
    clips = ClipStore(tmp_path / "cfg" / "clips.jsonl")
    app = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t", clips=clips)
    return TestClient(app, client=host), control, clips


def test_clip_inbox_routes(tmp_path: Path) -> None:
    client, control, _ = _client(tmp_path)
    body = {"url": "https://u:pw@example.org/tea?id=3#top", "title": "  Green \n tea ", "selection": "catechins"}
    assert client.post("/control/clips", json=body).status_code == 401
    r = client.post("/control/clips", json=body, headers={**H, "X-Operator": "chrome-extension"})
    assert r.status_code == 201
    item = r.json()
    assert item["url"] == "https://example.org/tea?id=3" and item["title"] == "Green tea" and item["by"] == "chrome-extension"
    assert client.post("/control/clips", json={**body, "url": "file:///etc/passwd"}, headers=H).status_code == 422
    assert client.post("/control/clips", json={**body, "extra": 1}, headers=H).status_code == 422
    listed = client.get("/control/clips", headers=H).json()
    assert [c["id"] for c in listed] == [item["id"]]
    audit = (tmp_path / "cfg" / "audit.jsonl").read_text(encoding="utf-8")
    assert "source_clipped" in audit and TOKEN not in audit and "catechins" not in audit


def test_clips_are_loopback_only(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, host=("192.168.1.20", 50000))
    assert client.post("/control/clips", json={"url": "https://example.org"}, headers=H).status_code == 403
    assert client.get("/control/clips", headers=H).status_code == 403


def test_extension_research_request_is_accepted(tmp_path: Path) -> None:
    client, control, _ = _client(tmp_path)
    params: dict[str, Any] = {"objective": "Research the topic of this page: Green tea", "mode": "offline",
                              "attachments": [{"kind": "url", "data": "https://example.org/tea", "name": "Green tea"}]}
    r = client.post("/control/runs", json={"params": params}, headers={**H, "X-Operator": "chrome-extension"})
    assert r.status_code == 201 and r.json()["run_id"]


def test_clip_store_cap_and_cleaning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("deep_research.control.clips.MAX_CLIPS", 3)
    store = ClipStore(tmp_path / "c.jsonl")
    for i in range(5):
        store.add(f"https://example.org/{i}")
    assert [c["url"][-1] for c in store.list()] == ["4", "3", "2"] and MAX_CLIPS == 500
    with pytest.raises(ValueError):
        clean_page_url("javascript:alert(1)")
    (tmp_path / "c.jsonl").write_text("not json\n[]\n", encoding="utf-8")
    assert store.list() == []


def test_server_wires_clip_store() -> None:
    assert "clips=ClipStore(" in (ROOT / "src/deep_research/control/server.py").read_text(encoding="utf-8")
