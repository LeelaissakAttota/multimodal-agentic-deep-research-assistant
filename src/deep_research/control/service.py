"""Wire the control runtime to the deep-research orchestrator."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import SecretStr

from deep_research.assistant.profile import apply_profile, read_profile
from deep_research.assistant.store import default_db_path
from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.domain.research_request import ResearchAttachment
from deep_research.control.runtime import AuditLog, ControlRun, ControlSettings, RunManager, Step
from deep_research.control.secrets_store import SecretStore, default_config_dir
from deep_research.core.config import Settings
from deep_research.core.orchestration.step_gate import GateOutcome
from deep_research.domain.research.research_task import ResearchTask

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
    def __init__(self, base_settings: Settings, config_dir: Path | None = None, assistant_db: Path | None = None) -> None:
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
        application = ResearchApplication(self.settings_with_keys(mode if isinstance(mode, str) else None))
        run.log(f"objective: {objective[:200]} (mode={application.settings.research_mode})")
        response = asyncio.run(
            application.submit(
                ResearchSubmission(objective=objective, attachments=_attachments(run.params.get("attachments"))),
                step_gate=ControlGate(run),
            )
        )
        if run.stopped:
            return response.model_dump(mode="json")
        if response.status not in ("completed",):
            run.log(f"research ended with status {response.status}: {response.error}", level="warning")
        return response.model_dump(mode="json")

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
