"""Notes CRUD and local reminders (SQLite in a temp dir)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from deep_research.assistant.notes import NotesService
from deep_research.assistant.store import AssistantStore


@pytest.fixture()
def svc(tmp_path: Path) -> NotesService:
    return NotesService(AssistantStore(tmp_path / "data" / "assistant.db"))


def test_note_add_edit_delete(svc: NotesService) -> None:
    n = svc.add_note("run1", "  check source [2]  ")
    assert n["text"] == "check source [2]" and n["run_id"] == "run1"
    svc.add_note("run2", "other")
    assert [x["text"] for x in svc.list_notes("run1")] == ["check source [2]"]
    edited = svc.edit_note(n["id"], "verified")
    assert edited["text"] == "verified"
    svc.delete_note(n["id"])
    assert svc.list_notes("run1") == [] and len(svc.list_notes()) == 1


def test_note_errors(svc: NotesService) -> None:
    with pytest.raises(ValueError):
        svc.add_note("r", "   ")
    with pytest.raises(ValueError):
        svc.add_note("r", "x" * 5001)
    with pytest.raises(KeyError):
        svc.edit_note(999, "x")
    with pytest.raises(KeyError):
        svc.delete_note(999)


def test_reminders_order_overdue_and_done(svc: NotesService) -> None:
    now = datetime(2026, 5, 1, 12, tzinfo=timezone.utc)
    later = svc.add_reminder("follow up", "2026-06-01", run_id="run1")
    past = svc.add_reminder("overdue one", date(2026, 4, 1))
    svc.add_reminder("tz aware", "2026-05-02T10:00+05:30")
    up = svc.upcoming(now=now)
    assert [r["text"] for r in up] == ["overdue one", "tz aware", "follow up"]
    assert up[0]["overdue"] and not up[2]["overdue"]
    assert up[1]["due"] == "2026-05-02T04:30+00:00"
    svc.set_done(past["id"])
    assert [r["text"] for r in svc.upcoming(now=now)] == ["tz aware", "follow up"]
    assert len(svc.list_reminders(include_done=True)) == 3
    assert [r["id"] for r in svc.list_reminders(run_id="run1")] == [later["id"]]
    svc.delete_reminder(later["id"])
    assert len(svc.list_reminders(include_done=True)) == 2


def test_reminder_validation(svc: NotesService) -> None:
    with pytest.raises(ValueError):
        svc.add_reminder("x", "next tuesday")
    with pytest.raises(ValueError):
        svc.add_reminder("", "2026-01-01")
    with pytest.raises(KeyError):
        svc.set_done(42)
    with pytest.raises(KeyError):
        svc.delete_reminder(42)
