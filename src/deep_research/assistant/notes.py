"""Notes on a report (add/edit/delete) and local reminders (no external notifications)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from deep_research.assistant.store import AssistantStore, now_iso

MAX_NOTE = 5_000
MAX_REMINDER = 300


def _text(value: str, limit: int) -> str:
    text = (value or "").strip()
    if not text:
        raise ValueError("text is empty")
    if len(text) > limit:
        raise ValueError(f"text is longer than {limit} characters")
    return text


def _due(value: str | date | datetime) -> str:
    """ISO date or datetime (stored as UTC ISO text so string order is time order)."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, 9, 0)
    else:
        try:
            dt = datetime.fromisoformat(value.strip())
        except (ValueError, AttributeError):
            raise ValueError("due must be an ISO date or datetime") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="minutes")


class NotesService:
    def __init__(self, store: AssistantStore) -> None:
        self.store = store

    # ----- notes -------------------------------------------------------------------------
    def add_note(self, run_id: str, text: str) -> dict[str, Any]:
        ts = now_iso()
        with self.store.connect() as db:
            cur = db.execute("INSERT INTO notes (run_id, text, created_at, updated_at) VALUES (?, ?, ?, ?)",
                             (run_id, _text(text, MAX_NOTE), ts, ts))
            note_id = int(cur.lastrowid or 0)
        return self.get_note(note_id)  # type: ignore[return-value]

    def get_note(self, note_id: int) -> dict[str, Any] | None:
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        return dict(row) if row else None

    def list_notes(self, run_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.connect() as db:
            if run_id is None:
                rows = db.execute("SELECT * FROM notes ORDER BY updated_at DESC, id DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM notes WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def edit_note(self, note_id: int, text: str) -> dict[str, Any]:
        with self.store.connect() as db:
            cur = db.execute("UPDATE notes SET text = ?, updated_at = ? WHERE id = ?", (_text(text, MAX_NOTE), now_iso(), note_id))
            if cur.rowcount == 0:
                raise KeyError(note_id)
        return self.get_note(note_id)  # type: ignore[return-value]

    def delete_note(self, note_id: int) -> None:
        with self.store.connect() as db:
            if db.execute("DELETE FROM notes WHERE id = ?", (note_id,)).rowcount == 0:
                raise KeyError(note_id)

    # ----- reminders ---------------------------------------------------------------------
    def add_reminder(self, text: str, due: str | date | datetime, run_id: str | None = None) -> dict[str, Any]:
        with self.store.connect() as db:
            cur = db.execute("INSERT INTO reminders (run_id, text, due, created_at) VALUES (?, ?, ?, ?)",
                             (run_id, _text(text, MAX_REMINDER), _due(due), now_iso()))
            rid = int(cur.lastrowid or 0)
            row = db.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        return self._reminder(dict(row))

    @staticmethod
    def _reminder(row: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        row["done"] = bool(row["done"])
        row["overdue"] = not row["done"] and datetime.fromisoformat(row["due"]) < now
        return row

    def list_reminders(self, *, include_done: bool = False, run_id: str | None = None,
                       now: datetime | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM reminders WHERE 1=1", []
        if not include_done:
            sql += " AND done = 0"
        if run_id is not None:
            sql += " AND run_id = ?"
            args.append(run_id)
        with self.store.connect() as db:
            rows = db.execute(sql + " ORDER BY due, id", args).fetchall()
        return [self._reminder(dict(r), now) for r in rows]

    def upcoming(self, limit: int = 5, now: datetime | None = None) -> list[dict[str, Any]]:
        """Open reminders by due date (overdue first) for the dashboard."""
        return self.list_reminders(now=now)[:limit]

    def set_done(self, reminder_id: int, done: bool = True) -> None:
        with self.store.connect() as db:
            if db.execute("UPDATE reminders SET done = ? WHERE id = ?", (int(done), reminder_id)).rowcount == 0:
                raise KeyError(reminder_id)

    def delete_reminder(self, reminder_id: int) -> None:
        with self.store.connect() as db:
            if db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,)).rowcount == 0:
                raise KeyError(reminder_id)
