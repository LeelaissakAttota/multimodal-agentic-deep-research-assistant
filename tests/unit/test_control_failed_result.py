"""A research that ends with status 'failed' must not show as a completed control run."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from deep_research.control.runtime import AuditLog, ControlSettings, RunManager, RunStatus


def _wait(run: Any) -> None:
    for _ in range(200):
        if run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED):
            return
        time.sleep(0.01)


def manager(tmp_path: Path, result: dict[str, Any]) -> RunManager:
    return RunManager(lambda run: result, lambda *a, **k: None, ControlSettings(tmp_path / "s.json"), AuditLog(tmp_path / "a.jsonl"))


def test_failed_research_marks_run_failed(tmp_path: Path) -> None:
    run = manager(tmp_path, {"status": "failed", "error": "Research wall-clock budget exhausted"}).create({"objective": "x"})
    _wait(run)
    assert run.status is RunStatus.FAILED and "budget" in (run.error or "")
    assert run.result["status"] == "failed"  # the partial result stays available


def test_completed_research_marks_run_completed(tmp_path: Path) -> None:
    run = manager(tmp_path, {"status": "completed"}).create({"objective": "x"})
    _wait(run)
    assert run.status is RunStatus.COMPLETED and run.error is None
