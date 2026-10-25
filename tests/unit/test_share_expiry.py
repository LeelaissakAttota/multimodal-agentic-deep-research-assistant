"""Share-link expiry: default TTL, per-share override, rejection like revoked, listing, migration."""

from __future__ import annotations

import sqlite3
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.assistant.share import ShareService
from deep_research.assistant.store import AssistantStore
from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings

TOKEN = "e" * 32
H = {"Authorization": f"Bearer {TOKEN}"}
REPORT: dict[str, Any] = {"title": "T", "content": "# T\n\nBody text [1].", "citations": [{"number": 1, "title": "S"}]}
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_default_ttl_is_seven_days(tmp_path: Path) -> None:
    svc = ShareService(AssistantStore(tmp_path / "a.db"))
    info, token = svc.create("r", REPORT, now=T0)
    assert info["expires_at"] == "2026-01-08T00:00:00+00:00"
    assert svc.open(info["id"], token, now=T0 + timedelta(days=6, hours=23)) is not None
    assert svc.open(info["id"], token, now=T0 + timedelta(days=7)) is None


def test_override_and_never(tmp_path: Path) -> None:
    svc = ShareService(AssistantStore(tmp_path / "a.db"), default_ttl_days=2)
    short, _ = svc.create("r", REPORT, now=T0)
    assert short["expires_at"].startswith("2026-01-03")
    long, _ = svc.create("r", REPORT, expires_in_days=30, now=T0)
    assert long["expires_at"].startswith("2026-01-31")
    never, tok = svc.create("r", REPORT, expires_in_days=0)
    assert never["expires_at"] is None and svc.open(never["id"], tok, now=T0 + timedelta(days=9999)) is not None
    for bad in (-1, 366, float("nan")):
        with pytest.raises(ValueError):
            svc.create("r", REPORT, expires_in_days=bad)


def test_expired_shown_in_list(tmp_path: Path) -> None:
    svc = ShareService(AssistantStore(tmp_path / "a.db"))
    info, _ = svc.create("r", REPORT, expires_in_days=1, now=datetime.now(timezone.utc) - timedelta(days=2))
    row = svc.list("r")[0]
    assert row["id"] == info["id"] and row["expired"] and not row["active"] and row["status"] == "expired"
    assert svc.info(info["id"])["status"] == "expired"  # type: ignore[index]


def test_old_database_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE shares (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, title TEXT NOT NULL, token_hash TEXT NOT NULL,"
                " html TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT)")
    con.execute("INSERT INTO shares VALUES ('oldshare01', 'r', 't', 'h', '<p>', '2026-01-01T00:00:00+00:00', NULL)")
    con.commit()
    con.close()
    svc = ShareService(AssistantStore(db))
    assert svc.info("oldshare01")["expires_at"] is None  # type: ignore[index]
    assert svc.create("r", REPORT)[0]["expires_at"]


def test_settings_ttl_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_RESEARCH_SHARE_TTL_DAYS", "3")
    assert Settings(_env_file=None).share_ttl_days == 3
    assert Settings(_env_file=None, share_ttl_days=0).share_ttl_days == 0
    with pytest.raises(ValueError):
        Settings(_env_file=None, share_ttl_days=-1)


def test_api_expiry_override_and_rejection(tmp_path: Path) -> None:
    store = AssistantStore(tmp_path / "a.db")
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path / "cfg", assistant_db=tmp_path / "a.db")
    client = TestClient(build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t",
                                          assistant=store, share_ttl_days=1))
    default = client.post("/control/runs/r1/shares", json={"report": REPORT}, headers=H).json()
    created = datetime.fromisoformat(default["created_at"])
    assert datetime.fromisoformat(default["expires_at"]) - created == timedelta(days=1)
    never = client.post("/control/runs/r1/shares", json={"report": REPORT, "expires_in_days": 0}, headers=H).json()
    assert never["expires_at"] is None
    assert client.post("/control/runs/r1/shares", json={"report": REPORT, "expires_in_days": 999}, headers=H).status_code == 422
    with store.connect() as db:
        db.execute("UPDATE shares SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (default["id"],))
    expired = client.get(default["path"])
    revoked_like = client.get(f"/share/{default['id']}?token=wrong")
    assert expired.status_code == revoked_like.status_code == 404 and expired.text == revoked_like.text
    assert client.get(never["path"]).status_code == 200
    listed = {s["id"]: s for s in client.get("/control/shares", headers=H).json()}
    assert listed[default["id"]]["status"] == "expired" and listed[never["id"]]["status"] == "active"
    token = default["path"].split("token=")[1]
    assert token not in str(control.manager.audit.tail(50))
