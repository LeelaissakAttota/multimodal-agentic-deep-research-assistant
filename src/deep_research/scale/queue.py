"""Research job queue: RQ on Redis when ``MDRA_REDIS_URL`` is set, otherwise in-process.

The control API keeps owning runs (status, logs, approvals). With a queue configured a run is
executed by a separate worker process (``deep-research-worker``) and the API polls the job.
Approval-mode runs stay in-process because approval gates need the API's memory.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Protocol

from deep_research.scale.config import ScaleSettings

JOB_FUNC = "deep_research.scale.jobs.run_research_job"
#: Job states shared by both queues (RQ's are mapped onto these).
TERMINAL = frozenset({"finished", "failed", "canceled"})


class JobQueue(Protocol):
    name: str

    def submit(self, payload: dict[str, Any]) -> str: ...

    def status(self, job_id: str) -> dict[str, Any]:
        """``{"status": queued|started|finished|failed|canceled, "result": ..., "error": str|None}``"""

    def cancel(self, job_id: str) -> None: ...


class InProcessQueue:
    """Default: jobs run on a small thread pool inside this process (no extra services)."""

    name = "in-process"

    def __init__(self, func: Callable[[dict[str, Any]], Any] | None = None, workers: int = 2) -> None:
        self._func = func
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mdra-job")
        self._jobs: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()

    def submit(self, payload: dict[str, Any]) -> str:
        from deep_research.scale.jobs import run_research_job

        func = self._func or run_research_job
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = self._pool.submit(func, payload)
        return job_id

    def status(self, job_id: str) -> dict[str, Any]:
        fut = self._jobs[job_id]
        if fut.cancelled():
            return {"status": "canceled", "result": None, "error": None}
        if not fut.done():
            return {"status": "started" if fut.running() else "queued", "result": None, "error": None}
        exc = fut.exception()
        if exc is not None:
            return {"status": "failed", "result": None, "error": f"{type(exc).__name__}: {exc}"}
        return {"status": "finished", "result": fut.result(), "error": None}

    def cancel(self, job_id: str) -> None:
        self._jobs[job_id].cancel()


def redis_connection(scale: ScaleSettings) -> Any:
    from redis import Redis

    return Redis.from_url(scale.secret("redis_url"), socket_connect_timeout=2, socket_timeout=10)


def ping(scale: ScaleSettings) -> None:
    redis_connection(scale).ping()


class RQQueue:
    """Jobs on an RQ queue; run them with ``deep-research-worker`` (SimpleWorker on Windows)."""

    name = "rq"

    def __init__(self, scale: ScaleSettings, connection: Any | None = None) -> None:
        from rq import Queue

        self.scale = scale
        self.connection = connection if connection is not None else redis_connection(scale)
        self.queue = Queue(scale.rq_queue, connection=self.connection)

    def submit(self, payload: dict[str, Any]) -> str:
        job = self.queue.enqueue(JOB_FUNC, payload, job_timeout=self.scale.job_timeout_seconds,
                                 result_ttl=86_400, failure_ttl=86_400, description="mdra research run")
        return str(job.id)

    def status(self, job_id: str) -> dict[str, Any]:
        from rq.job import Job

        job = Job.fetch(job_id, connection=self.connection)
        raw = str(getattr(job.get_status(refresh=True), "value", job.get_status()))
        status = {"queued": "queued", "deferred": "queued", "scheduled": "queued", "started": "started",
                  "finished": "finished", "failed": "failed", "stopped": "canceled", "canceled": "canceled"}.get(raw, raw)
        error = None
        if status == "failed":
            latest = job.latest_result()
            info = str(getattr(latest, "exc_string", "") or "").strip().splitlines()
            error = info[-1][:500] if info else "job failed"
        return {"status": status, "result": job.return_value() if status == "finished" else None, "error": error}

    def cancel(self, job_id: str) -> None:
        from rq.command import send_stop_job_command
        from rq.job import Job

        job = Job.fetch(job_id, connection=self.connection)
        if job.get_status() == "started":
            send_stop_job_command(self.connection, job_id)
        else:
            job.cancel()


def build_queue(scale: ScaleSettings | None = None) -> JobQueue | None:
    """RQ queue when ``MDRA_REDIS_URL`` is set and ``rq`` is installed; ``None`` = keep in-process runs."""
    s = scale or ScaleSettings()
    if not s.secret("redis_url"):
        return None
    from deep_research.scale.config import has_module

    if not (has_module("rq") and has_module("redis")):
        raise RuntimeError("MDRA_REDIS_URL is set but the 'queue' extra (rq, redis) is not installed")
    return RQQueue(s)


def needs_simple_worker() -> bool:
    """RQ's default Worker forks per job; Windows has no ``os.fork``."""
    return os.name == "nt" or not hasattr(os, "fork")
