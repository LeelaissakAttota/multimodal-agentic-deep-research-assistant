"""Optional RQ/Redis job queue: off by default, fakeredis end-to-end, in-process fallback, Windows worker."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from deep_research.control.runtime import RunStatus
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.scale import backend_status
from deep_research.scale.config import ScaleSettings
from deep_research.scale.queue import InProcessQueue, build_queue, needs_simple_worker
from deep_research.scale.worker import main as worker_main


def _wait(run: Any, timeout: float = 30) -> None:
    end = time.time() + timeout
    while run.status not in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.STOPPED) and time.time() < end:
        time.sleep(0.05)


def _control(tmp_path: Path, **kw: Any) -> DeepResearchControl:
    return DeepResearchControl(Settings(_env_file=None, research_mode="offline"), config_dir=tmp_path / "cfg",
                               assistant_db=tmp_path / "a.db", **kw)


def test_off_by_default(tmp_path: Path) -> None:
    assert ScaleSettings().secret("redis_url") == ""
    assert build_queue() is None
    control = _control(tmp_path)
    assert control.queue is None and control.queue_error is None
    row = backend_status()[0]
    assert row["state"] == "off" and "in-process" in row["detail"]


def test_status_never_shows_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_REDIS_URL", "redis://:hunter2secret@redis.internal:6379/0")
    rows = backend_status()
    assert rows[0]["configured"] and "hunter2secret" not in str(rows) and "redis.internal" not in str(rows)
    assert "hunter2secret" not in repr(ScaleSettings())


def test_missing_extra_falls_back_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import deep_research.scale.config as cfg

    monkeypatch.setenv("MDRA_REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setattr(cfg, "has_module", lambda name: False)
    control = _control(tmp_path)
    assert control.queue is None and "queue" in (control.queue_error or "")
    assert backend_status()[0]["state"] == "missing-dependency"
    run = control.manager.create({"objective": "What is green tea?"})
    _wait(run)
    assert run.status == RunStatus.COMPLETED


def test_in_process_queue_runs_job(tmp_path: Path) -> None:
    q = InProcessQueue(lambda p: {"echo": p["objective"]})
    job = q.submit({"objective": "x"})
    for _ in range(100):
        if q.status(job)["status"] == "finished":
            break
        time.sleep(0.01)
    assert q.status(job) == {"status": "finished", "result": {"echo": "x"}, "error": None}
    bad = InProcessQueue(lambda p: 1 / 0)
    jid = bad.submit({})
    time.sleep(0.1)
    assert bad.status(jid)["status"] == "failed" and "ZeroDivisionError" in bad.status(jid)["error"]


def test_control_uses_injected_queue_and_respects_approval_mode(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def job(payload: dict[str, Any]) -> dict[str, Any]:
        seen.append(payload)
        return {"status": "completed", "via": "queue"}

    control = _control(tmp_path, queue=InProcessQueue(job))
    run = control.manager.create({"objective": "Tea", "mode": "offline"})
    _wait(run)
    assert run.status == RunStatus.COMPLETED and run.result == {"status": "completed", "via": "queue"}
    assert seen == [{"objective": "Tea", "mode": "offline", "attachments": [], "run_id": run.run_id}]
    assert any("queued as job" in line["message"] for line in run.logs)
    control.manager.settings.update(approval_mode=True)
    run2 = control.manager.create({"objective": "Tea"})
    time.sleep(0.3)
    assert len(seen) == 1 and any("not sent to the job queue" in line["message"] for line in run2.logs)
    run2.stop()


def test_rq_fakeredis_end_to_end(tmp_path: Path) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    rq = pytest.importorskip("rq")
    from deep_research.scale.queue import RQQueue

    conn = fakeredis.FakeStrictRedis()
    queue = RQQueue(ScaleSettings(redis_url="redis://fake:6379/0"), connection=conn)
    job_id = queue.submit({"objective": "What are the health benefits of green tea?", "mode": "offline"})
    assert queue.status(job_id)["status"] == "queued"
    rq.SimpleWorker([queue.queue], connection=conn).work(burst=True)
    state = queue.status(job_id)
    assert state["status"] == "finished", state
    assert state["result"]["status"] == "completed" and state["result"]["report"]["content"]
    bad = queue.submit({"objective": ""})
    rq.SimpleWorker([queue.queue], connection=conn).work(burst=True)
    failed = queue.status(bad)
    assert failed["status"] == "failed" and "objective is required" in failed["error"]
    pending = queue.submit({"objective": "x"})
    queue.cancel(pending)
    assert queue.status(pending)["status"] == "canceled"


def test_worker_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert worker_main([]) == 2 and "in-process" in capsys.readouterr().out
    monkeypatch.setattr(os, "name", "nt")
    assert needs_simple_worker()


@pytest.mark.skipif(not os.environ.get("MDRA_TEST_REDIS_URL"), reason="set MDRA_TEST_REDIS_URL to run against real Redis")
def test_real_redis_roundtrip() -> None:
    from rq import SimpleWorker

    from deep_research.scale.queue import RQQueue

    queue = RQQueue(ScaleSettings(redis_url=os.environ["MDRA_TEST_REDIS_URL"], rq_queue="mdra-test"))
    job_id = queue.submit({"objective": "What is green tea?", "mode": "offline"})
    SimpleWorker([queue.queue], connection=queue.connection).work(burst=True)
    assert queue.status(job_id)["status"] == "finished"


def test_unreachable_queue_at_enqueue_runs_in_process(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    class DownQueue(InProcessQueue):
        name = "rq"

        def submit(self, payload: dict[str, Any]) -> str:
            raise ConnectionError("Error 111 connecting to redis://:hunter2secret@redis.internal:6379")

    control = _control(tmp_path, queue=DownQueue())
    with caplog.at_level("WARNING"):
        run = control.manager.create({"objective": "What is green tea?", "mode": "offline"})
        _wait(run)
    assert run.status == RunStatus.COMPLETED
    messages = " ".join(line["message"] for line in run.logs)
    assert "unreachable" in messages and "in-process" in messages and "queued as job" not in messages
    assert control.queue_fallback == "ConnectionError"
    assert "running this job in-process" in caplog.text
    assert "hunter2secret" not in messages + caplog.text + str(run.logs)
