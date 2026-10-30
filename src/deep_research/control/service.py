"""Wire the control runtime to the deep-research orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from deep_research.assistant.profile import apply_profile, read_profile
from deep_research.assistant.store import default_db_path
from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.domain.research_request import ResearchAttachment
from deep_research.control.runtime import AuditLog, ControlRun, ControlSettings, RunManager, RunStopped, Step
from deep_research.control.secrets_store import SecretStore, default_config_dir
from deep_research.core.config import Settings
from deep_research.core.orchestration.step_gate import GateOutcome
from deep_research.domain.research.research_task import ResearchTask
from deep_research.scale.config import ScaleSettings
from deep_research.scale.queue import JobQueue, build_queue

logger = logging.getLogger(__name__)

#: Env-style key name -> Settings field.
KEY_FIELDS: dict[str, str] = {
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "TAVILY_API_KEY": "tavily_api_key",
    "NVIDIA_API_KEY": "nvidia_api_key",
    "HUGGINGFACE_API_KEY": "huggingface_api_key",
}


class ControlGate:
    """Async adapter: blocks (in a worker thread) on the run's approval gate."""

    def __init__(self, run: ControlRun) -> None:
        self._run = run

    @property
    def parallel_safe(self) -> bool:
        """Independent research tasks may run concurrently only when no human approves each step."""
        return not self._run.approval_mode

    async def before(self, kind: str, name: str, inputs: dict[str, Any]) -> GateOutcome:
        step, decision = await asyncio.to_thread(self._run.gate, kind, name, inputs)
        if decision.approved:
            self._run.step_started(step)
        return GateOutcome(approved=decision.approved, inputs=decision.inputs, stopped=decision.stopped, handle=step)

    async def after(self, outcome: GateOutcome, *, ok: bool, output: Any = None, error: str | None = None) -> None:
        if isinstance(outcome.handle, Step) and outcome.approved:
            self._run.step_finished(outcome.handle, ok=ok, output=output, error=error)


    def trace(self, events: list[dict[str, Any]]) -> None:
        """Real progress events from the orchestrator -> run log (redacted), for the live research view."""

        for ev in events[:40]:
            kind = str(ev.get("event", "info"))
            if kind == "plan":
                text = f"plan task: {ev.get('task')}" + (f" [{ev.get('tool')}]" if ev.get("tool") else "")
            elif kind == "search":
                text = f"search: {ev.get('query')}"
            elif kind == "source":
                text = f"source: {ev.get('title') or ev.get('url')}" + (f" <{ev.get('url')}>" if ev.get("url") else "")
            elif kind == "verdict":
                gaps = "; ".join(ev.get("gaps") or [])
                text = f"evaluator: {ev.get('decision')}" + (f" (gaps: {gaps})" if gaps else "")
            else:
                text = kind
            self._run.log(text[:600], **{k: v for k, v in ev.items() if k != "gaps"})


class DeepResearchControl:
    def __init__(self, base_settings: Settings, config_dir: Path | None = None, assistant_db: Path | None = None,
                 *, with_queue: bool = True, queue: JobQueue | None = None) -> None:
        self.config_dir = config_dir or default_config_dir("deep-research")
        self.base_settings = base_settings
        self.assistant_db = assistant_db or default_db_path(base_settings.memory_dir)
        self.secrets = SecretStore(self.config_dir / "secrets.json", tuple(KEY_FIELDS))
        self.manager = RunManager(
            self._worker,
            self._rerun,
            ControlSettings(self.config_dir / "control_settings.json"),
            AuditLog(self.config_dir / "audit.jsonl"),
        )
        # Optional background queue (MDRA_REDIS_URL); None = in-process runs exactly as before.
        self.queue: JobQueue | None = queue
        self.queue_error: str | None = None
        #: Last enqueue failure ("<ExcType>"), set while runs are falling back to in-process; cleared on success.
        self.queue_fallback: str | None = None
        if queue is None and with_queue:
            try:
                self.queue = build_queue()
            except Exception as exc:  # missing extra or bad URL: keep working in-process, report it
                self.queue_error = f"{type(exc).__name__}: {str(exc)[:200]}" if isinstance(exc, RuntimeError) else type(exc).__name__

    def settings_with_keys(self, mode: str | None = None) -> Settings:
        """Fresh settings per run: keys re-read from the secrets file (no restart)."""
        update: dict[str, Any] = {}
        for name, field_name in KEY_FIELDS.items():
            value = self.secrets.get(name)
            if value:
                update[field_name] = SecretStr(value)
        if mode in ("offline", "live"):
            update["research_mode"] = mode
        return apply_profile(self.base_settings.model_copy(update=update), read_profile(self.assistant_db))

    def _worker(self, run: ControlRun) -> dict[str, Any]:
        objective = str(run.params.get("objective") or "").strip()
        if not objective:
            raise ValueError("params.objective is required")
        mode = run.params.get("mode")
        if self.queue is not None:
            if not run.approval_mode:
                queued = self._run_queued(run, self.queue, objective)
                if queued is not None:
                    return queued
            else:
                run.log("approval mode needs the in-process runner; this run is not sent to the job queue")
        settings, assignment = run_variant(self.settings_with_keys(mode if isinstance(mode, str) else None), run.run_id)
        if assignment:
            run.log(f"A/B experiment {assignment['experiment']}: variant {assignment['variant']}")
        application = ResearchApplication(settings)
        run.log(f"objective: {objective[:200]} (mode={application.settings.research_mode})")
        response = asyncio.run(
            application.submit(
                ResearchSubmission(objective=objective, attachments=_attachments(run.params.get("attachments"))),
                step_gate=ControlGate(run),
                run_id=run.run_id,
            )
        )
        if response.status not in ("completed",) and not run.stopped:
            run.log(f"research ended with status {response.status}: {response.error}", level="warning")
        result = response.model_dump(mode="json")
        from deep_research.scale.graphrag import auto_sync

        if auto_sync(run.run_id, result, objective[:200]):
            run.log("knowledge graph synced to Neo4j")
        if assignment:
            result["experiment"] = assignment
        return result

    def _run_queued(self, run: ControlRun, queue: JobQueue, objective: str) -> dict[str, Any] | None:
        """Enqueue the run for a worker and poll it; stop requests cancel the job.

        Returns ``None`` when the queue cannot accept the job (e.g. Redis down): the caller then runs it in-process.
        """
        payload = {"objective": objective, "mode": run.params.get("mode"), "attachments": run.params.get("attachments") or [],
                   "run_id": run.run_id}
        _attachments(payload["attachments"])  # validate before queueing
        try:
            job_id = queue.submit(payload)
        except Exception as exc:  # unreachable broker: degrade, never fail the run. Type only - messages may hold URLs.
            self.queue_fallback = type(exc).__name__
            msg = (f"job queue '{queue.name}' is unreachable ({type(exc).__name__}); "
                   "running this job in-process instead (start Redis or unset MDRA_REDIS_URL)")
            logger.warning(msg)
            run.log(msg, level="warning", queue_fallback=True)
            return None
        self.queue_fallback = None
        run.log(f"queued as job {job_id} on the {queue.name} queue", job_id=job_id)
        last = ""
        poll = ScaleSettings().job_poll_seconds
        while True:
            state = queue.status(job_id)
            if state["status"] != last:
                last = state["status"]
                run.log(f"job {last}", job_id=job_id)
            if state["status"] == "finished":
                result = state["result"]
                return result if isinstance(result, dict) else {"result": result}
            if state["status"] in ("failed", "canceled"):
                if run.stopped:
                    raise RunStopped()
                raise RuntimeError(state.get("error") or f"job {state['status']}")
            if run.stopped:
                queue.cancel(job_id)
                raise RunStopped()
            time.sleep(poll)

    def _rerun(self, run: ControlRun, step: Step, inputs: dict[str, Any]) -> tuple[bool, Any, str | None]:
        if step.kind != "research_task":
            return False, None, "re-run is supported for research_task steps only"
        mode = run.params.get("mode")
        application = ResearchApplication(self.settings_with_keys(mode if isinstance(mode, str) else None))
        agent = application.build_research_agent()
        objective = str(inputs.get("objective") or "").strip()
        task = ResearchTask(
            plan_id=uuid4(),
            description=str(inputs.get("description") or objective)[:500] or "re-run",
            objective=objective or "re-run",
            assigned_tool=inputs.get("tool") if isinstance(inputs.get("tool"), str) else None,
        )
        result = asyncio.run(agent.execute_task(task))
        output = result.output if isinstance(result.output, dict) else {}
        return bool(result.success), output.get("summary"), result.error


def run_variant(settings: Settings, run_id: str) -> tuple[Settings, dict[str, Any] | None]:
    """Apply the configured A/B variant for ``run_id`` (no-op without ``MDRA_AB_CONFIG``)."""
    from deep_research.scale.experiments import active_experiment, apply_variant

    return apply_variant(settings, run_id, active_experiment())


def _attachments(raw: Any) -> list[ResearchAttachment]:
    """Validate optional ``params.attachments`` ([{kind, data, name}]) from the dashboard."""

    if not raw:
        return []
    if not isinstance(raw, list):
        raise ValueError("params.attachments must be a list")
    return [ResearchAttachment.model_validate(item) for item in raw[:5]]


def admin_token_from_env() -> str:
    token = os.environ.get("DEEP_RESEARCH_CONTROL_TOKEN", "")
    if not token:
        raise SystemExit("DEEP_RESEARCH_CONTROL_TOKEN must be set (>=16 chars)")
    return token
