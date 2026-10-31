"""Local SQLite storage for assistant features (chat history, profile, notes, reminders, shares).

No secrets are stored: share links keep only a SHA-256 hash of their token.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, role TEXT NOT NULL,
    text TEXT NOT NULL, meta TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, text TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, text TEXT NOT NULL,
    due TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, title TEXT NOT NULL,
    token_hash TEXT NOT NULL, html TEXT NOT NULL, created_at TEXT NOT NULL, revoked_at TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path(memory_dir: str | os.PathLike[str] | None = None) -> Path:
    """``DEEP_RESEARCH_ASSISTANT_DB`` or ``<memory_dir>/assistant.db`` (``data/`` is git-ignored)."""
    override = os.environ.get("DEEP_RESEARCH_ASSISTANT_DB")
    if override:
        return Path(override)
    return Path(memory_dir or "data") / "assistant.db"


class AssistantStore:
    """Thread-safe, small SQLite wrapper; opens a short-lived connection per operation."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(_SCHEMA)
            cols = {r["name"] for r in db.execute("PRAGMA table_info(shares)").fetchall()}
            if "expires_at" not in cols:  # databases created before share expiry existed
                db.execute("ALTER TABLE shares ADD COLUMN expires_at TEXT")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            try:
                yield db
                db.commit()
            finally:
                db.close()

    # ----- chat --------------------------------------------------------------------------
    def add_chat(self, run_id: str, role: str, text: str, meta: dict[str, Any] | None = None) -> int:
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        with self.connect() as db:
            cur = db.execute("INSERT INTO chat (run_id, role, text, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                             (run_id, role, text, json.dumps(meta or {}), now_iso()))
            return int(cur.lastrowid or 0)

    def chat_history(self, run_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM chat WHERE run_id = ? ORDER BY id DESC LIMIT ?", (run_id, limit)).fetchall()
        return [{**dict(r), "meta": json.loads(r["meta"])} for r in reversed(rows)]

    # ----- key/value (profile) -----------------------------------------------------------
    def get_json(self, key: str) -> Any:
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def put_json(self, key: str, value: Any) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (key, json.dumps(value)))
