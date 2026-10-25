"""Supervised run control: human-in-the-loop gates, pause/stop, audit log.

The engine calls :meth:`ControlRun.gate` immediately *before* a step executes.
When approval mode is on the call blocks the worker thread until an operator
approves (optionally overriding inputs), rejects, or stops the run. With
``auto_approve_low_risk`` on, steps classified LOW are approved automatically;
HIGH-risk steps always wait for an explicit human decision.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .risk import RiskLevel, classify_step
from deep_research.observability.redaction import redact_text, redact_value

MAX_LOG_LINES = 2000


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class StepState(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class DecisionKind(StrEnum):
    AUTO = "auto"  # auto-approved low-risk step
    MANUAL = "manual"  # explicit human approval
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"  # approval mode off
    STOPPED = "stopped"


@dataclass
class GateResult:
    approved: bool
    inputs: dict[str, Any]
    stopped: bool = False
    note: str | None = None


@dataclass
class Step:
    step_id: str
    index: int
    kind: str
    name: str
    inputs: dict[str, Any]
    risk: RiskLevel
    state: StepState = StepState.PENDING_APPROVAL
    decision: DecisionKind | None = None
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None
    overridden: bool = False
    output: Any = None
    error: str | None = None
    attempts: int = 0
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "index": self.index,
            "kind": self.kind,
            "name": self.name,
            "inputs": self.inputs,
            "risk": self.risk.value,
            "state": self.state.value,
            "decision": self.decision.value if self.decision else None,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "note": self.note,
            "overridden": self.overridden,
            "output": self.output,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


#: Optional env default for "Step-by-step approval" used until it is saved in control_settings.json.
APPROVAL_ENV = "DEEP_RESEARCH_APPROVAL_MODE"


def env_approval_default() -> bool:
    """``DEEP_RESEARCH_APPROVAL_MODE`` from the environment or ``.env``, read like the main ``Settings``.

    Same pydantic-settings rules: ``DEEP_RESEARCH_`` prefix, case-insensitive, real env vars beat ``.env``,
    ``DEEP_RESEARCH_ENV_FILE`` picks the file (evaluated per call, so tests can point at a tmp file).
    """
    from pydantic import ValidationError
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class _ApprovalEnv(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="DEEP_RESEARCH_", case_sensitive=False, extra="ignore",
                                          env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
                                          env_file_encoding="utf-8")
        approval_mode: bool = False

    try:
        return _ApprovalEnv().approval_mode
    except ValidationError:  # unparsable value: stay safe and use the default (off)
        return False


class ControlSettings:
    """Persisted per-project control toggles (default OFF), stored as JSON in the control config dir.

    ``approval_mode`` ("Step-by-step approval") precedence: saved value in this file, then
    ``DEEP_RESEARCH_APPROVAL_MODE`` from env/.env (:func:`env_approval_default`), then off.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def load(self) -> dict[str, bool]:
        data: dict[str, Any] = {}
        with contextlib.suppress(FileNotFoundError, ValueError):
            data = json.loads(self._path.read_text(encoding="utf-8"))
        return {
            "approval_mode": bool(data["approval_mode"]) if "approval_mode" in data else env_approval_default(),
            "auto_approve_low_risk": bool(data.get("auto_approve_low_risk", False)),
        }

    def update(self, **changes: bool | None) -> dict[str, bool]:
        with self._lock:
            current = self.load()
            for key, value in changes.items():
                if value is not None and key in current:
                    current[key] = bool(value)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(current), encoding="utf-8")
            return current


class AuditLog:
    """Append-only JSONL audit of approval decisions and key changes (never values)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def record(self, event: dict[str, Any]) -> dict[str, Any]:
        entry = {"at": _now(), **event}
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        return entry

    def tail(self, limit: int = 200, run_id: str | None = None) -> list[dict[str, Any]]:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in reversed(lines):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if run_id and item.get("run_id") != run_id:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out


class RunStopped(Exception):
    """Raised inside a worker when the operator stopped the run."""


class ControlRun:
    def __init__(
        self,
        run_id: str,
        params: dict[str, Any],
        *,
        settings: dict[str, bool],
        audit: AuditLog,
        approval_mode: bool | None = None,
    ) -> None:
        self.run_id = run_id
        self.params = params
        #: Per-run override wins over the saved setting; ``approval_source`` says which one applied.
        self.approval_mode = settings["approval_mode"] if approval_mode is None else bool(approval_mode)
        self.approval_source = "settings" if approval_mode is None else "run"
        self.auto_approve_low_risk = settings["auto_approve_low_risk"]
        self.audit = audit
        self.status = RunStatus.QUEUED
        self.steps: list[Step] = []
        self.logs: list[dict[str, Any]] = []
        self.result: Any = None
        self.error: str | None = None
        self.created_at = _now()
        self.finished_at: str | None = None
        self._cond = threading.Condition()
        self._paused = False
        self._stopped = False
        self._decisions: dict[str, GateResult] = {}

    # ----- logging -------------------------------------------------------------
    def log(self, message: str, level: str = "info", **extra: Any) -> None:
        safe_extra = {key: redact_value(key, value) for key, value in extra.items()}
        with self._cond:
            self.logs.append(
                {
                    "seq": len(self.logs),
                    "at": _now(),
                    "level": level,
                    "message": redact_text(message),
                    **safe_extra,
                }
            )
            if len(self.logs) > MAX_LOG_LINES:
                del self.logs[: len(self.logs) - MAX_LOG_LINES]
            self._cond.notify_all()

    # ----- lifecycle ------------------------------------------------------------
    @property
    def stopped(self) -> bool:
        return self._stopped

    def pause(self) -> None:
        with self._cond:
            if self.status in _TERMINAL:
                raise ValueError("run is finished")
            self._paused = True
            if self.status is not RunStatus.WAITING_APPROVAL:
                self.status = RunStatus.PAUSED
            self._cond.notify_all()
        self.log("run paused by operator")

    def resume(self) -> None:
        with self._cond:
            if self.status in _TERMINAL:
                raise ValueError("run is finished")
            self._paused = False
            if self.status is RunStatus.PAUSED:
                self.status = RunStatus.RUNNING
            self._cond.notify_all()
        self.log("run resumed by operator")

    def stop(self) -> None:
        with self._cond:
            if self.status in _TERMINAL:
                return
            self._stopped = True
            self._paused = False
            self.status = RunStatus.STOPPING
            self._cond.notify_all()
        self.log("stop requested by operator", level="warning")

    def finish(self, *, result: Any = None, error: str | None = None) -> None:
        with self._cond:
            self.result = result
            self.error = error
            if self._stopped:
                self.status = RunStatus.STOPPED
            elif error:
                self.status = RunStatus.FAILED
            else:
                self.status = RunStatus.COMPLETED
            self.finished_at = _now()
            self._cond.notify_all()
        self.log(f"run finished: {self.status.value}", level="error" if error else "info")

    # ----- gates ---------------------------------------------------------------
    def gate(self, kind: str, name: str, inputs: dict[str, Any]) -> tuple[Step, GateResult]:
        """Register a step and block until it may execute (or is rejected/stopped)."""

        with self._cond:
            while self._paused and not self._stopped:
                self.status = RunStatus.PAUSED
                self._cond.wait(timeout=0.5)
            step = Step(
                step_id=uuid.uuid4().hex[:12],
                index=len(self.steps),
                kind=kind,
                name=name,
                inputs=dict(inputs),
                risk=classify_step(kind),
            )
            self.steps.append(step)
            if self._stopped:
                self._decide(step, DecisionKind.STOPPED, "system", None)
                return step, GateResult(False, step.inputs, stopped=True)
            if not self.approval_mode:
                self._decide(step, DecisionKind.NOT_REQUIRED, "policy:approval_mode_off", None)
                return step, GateResult(True, step.inputs)
            if self.auto_approve_low_risk and step.risk is RiskLevel.LOW:
                self._decide(step, DecisionKind.AUTO, "policy:auto_approve_low_risk", None)
                return step, GateResult(True, step.inputs)
            self.status = RunStatus.WAITING_APPROVAL
            self._cond.notify_all()
        self.log(f"step {step.index} '{name}' ({step.risk.value} risk) awaiting approval", step_id=step.step_id)
        with self._cond:
            while step.step_id not in self._decisions and not self._stopped:
                self._cond.wait(timeout=0.5)
            if step.step_id not in self._decisions:
                self._decide(step, DecisionKind.STOPPED, "system", None)
                return step, GateResult(False, step.inputs, stopped=True)
            decision = self._decisions.pop(step.step_id)
            self.status = RunStatus.PAUSED if self._paused else RunStatus.RUNNING
            return step, decision

    def _decide(self, step: Step, kind: DecisionKind, who: str, note: str | None) -> None:
        step.decision = kind
        step.decided_by = who
        step.decided_at = _now()
        step.note = note
        if kind in (DecisionKind.REJECTED, DecisionKind.STOPPED):
            step.state = StepState.REJECTED if kind is DecisionKind.REJECTED else StepState.SKIPPED
        else:
            step.state = StepState.APPROVED
        if kind is not DecisionKind.NOT_REQUIRED:
            self.audit.record(
                {
                    "event": "step_decision",
                    "run_id": self.run_id,
                    "step_id": step.step_id,
                    "step_index": step.index,
                    "step_name": step.name,
                    "kind": step.kind,
                    "risk": step.risk.value,
                    "decision": kind.value,
                    "by": who,
                    "note": note,
                    "overridden": step.overridden,
                }
            )

    def _pending(self, step_id: str) -> Step:
        for step in self.steps:
            if step.step_id == step_id:
                if step.state is not StepState.PENDING_APPROVAL:
                    raise ValueError("step is not awaiting approval")
                return step
        raise KeyError(step_id)

    def approve(
        self, step_id: str, *, by: str, note: str | None = None, inputs: dict[str, Any] | None = None
    ) -> Step:
        with self._cond:
            step = self._pending(step_id)
            if inputs is not None:
                step.inputs = {**step.inputs, **inputs}
                step.overridden = True
            self._decide(step, DecisionKind.MANUAL, by, note)
            self._decisions[step_id] = GateResult(True, dict(step.inputs), note=note)
            self._cond.notify_all()
        self.log(f"step {step.index} approved by {by}" + (" (inputs overridden)" if step.overridden else ""))
        return step

    def reject(self, step_id: str, *, by: str, note: str | None = None) -> Step:
        with self._cond:
            step = self._pending(step_id)
            self._decide(step, DecisionKind.REJECTED, by, note)
            self._decisions[step_id] = GateResult(False, dict(step.inputs), note=note)
            self._cond.notify_all()
        self.log(f"step {step.index} rejected by {by}", level="warning")
        return step

    def step_started(self, step: Step) -> None:
        step.state = StepState.RUNNING
        step.attempts += 1
        self.log(f"step {step.index} '{step.name}' running", step_id=step.step_id)

    def step_finished(self, step: Step, *, ok: bool, output: Any = None, error: str | None = None) -> None:
        step.state = StepState.SUCCEEDED if ok else StepState.FAILED
        step.output = output
        step.error = error
        step.finished_at = _now()
        self.log(
            f"step {step.index} '{step.name}' {'succeeded' if ok else 'failed'}" + (f": {error}" if error else ""),
            level="info" if ok else "error",
            step_id=step.step_id,
        )

    def find(self, step_id: str) -> Step:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    def wait_for_logs(self, after: int, timeout: float) -> list[dict[str, Any]]:
        with self._cond:
            if not [x for x in self.logs if x["seq"] >= after] and self.status not in _TERMINAL:
                self._cond.wait(timeout=timeout)
            return [x for x in self.logs if x["seq"] >= after]

    def to_dict(self, *, include_steps: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status.value,
            "params": self.params,
            "approval_mode": self.approval_mode,
            "approval_source": self.approval_source,
            "auto_approve_low_risk": self.auto_approve_low_risk,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "pending_approvals": [s.step_id for s in self.steps if s.state is StepState.PENDING_APPROVAL],
            "step_count": len(self.steps),
        }
        if include_steps:
            data["steps"] = [s.to_dict() for s in self.steps]
            data["result"] = self.result
        return data


_TERMINAL = frozenset({RunStatus.STOPPED, RunStatus.COMPLETED, RunStatus.FAILED})

Worker = Callable[[ControlRun], Any]
Rerunner = Callable[[ControlRun, Step, dict[str, Any]], tuple[bool, Any, str | None]]


class RunManager:
    """Owns runs; each run executes in its own daemon thread."""

    def __init__(
        self,
        worker: Worker,
        rerunner: Rerunner,
        settings: ControlSettings,
        audit: AuditLog,
        *,
        max_runs: int = 50,
    ) -> None:
        self._worker = worker
        self._rerunner = rerunner
        self.settings = settings
        self.audit = audit
        self._runs: dict[str, ControlRun] = {}
        self._max_runs = max_runs
        self._lock = threading.Lock()

    def create(self, params: dict[str, Any], *, approval_mode: bool | None = None) -> ControlRun:
        """Start a run. ``approval_mode`` overrides the saved "Step-by-step approval" setting for this run only."""
        run = ControlRun(uuid.uuid4().hex[:16], params, settings=self.settings.load(), audit=self.audit,
                         approval_mode=approval_mode)
        with self._lock:
            self._runs[run.run_id] = run
            while len(self._runs) > self._max_runs:
                oldest = next(iter(self._runs))
                if self._runs[oldest].status not in _TERMINAL:
                    break
                del self._runs[oldest]
        self.audit.record({"event": "run_created", "run_id": run.run_id,
                           "approval_mode": run.approval_mode, "approval_source": run.approval_source,
                           "auto_approve_low_risk": run.auto_approve_low_risk})
        thread = threading.Thread(target=self._execute, args=(run,), daemon=True, name=f"run-{run.run_id}")
        thread.start()
        return run

    def _execute(self, run: ControlRun) -> None:
        run.status = RunStatus.RUNNING
        run.log("run started")
        try:
            result = self._worker(run)
        except RunStopped:
            run.finish(result=None)
        except Exception as exc:  # surfaced to the dashboard, never re-raised
            run.finish(error=f"{type(exc).__name__}: {exc}")
        else:
            run.finish(result=result)

    def get(self, run_id: str) -> ControlRun:
        return self._runs[run_id]

    def list(self) -> list[ControlRun]:
        return list(reversed(list(self._runs.values())))

    def retry(self, run_id: str, step_id: str, *, by: str, inputs: dict[str, Any] | None) -> Step:
        run = self.get(run_id)
        original = run.find(step_id)
        if original.state in (StepState.PENDING_APPROVAL, StepState.RUNNING):
            raise ValueError("step is not finished")
        merged = {**original.inputs, **(inputs or {})}
        with run._cond:
            step = Step(
                step_id=uuid.uuid4().hex[:12],
                index=len(run.steps),
                kind=original.kind,
                name=f"{original.name} (re-run of #{original.index})",
                inputs=merged,
                risk=original.risk,
                overridden=bool(inputs),
            )
            run.steps.append(step)
            run._decide(step, DecisionKind.MANUAL, by, f"re-run of step {original.step_id}")
        run.step_started(step)
        try:
            ok, output, error = self._rerunner(run, step, merged)
        except Exception as exc:
            ok, output, error = False, None, f"{type(exc).__name__}: {exc}"
        run.step_finished(step, ok=ok, output=output, error=error)
        return step
