"""Optional pre-execution gate used by supervised control (human-in-the-loop)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class GateOutcome:
    approved: bool
    inputs: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False
    handle: Any = None


class StepGate(Protocol):
    async def before(self, kind: str, name: str, inputs: dict[str, Any]) -> GateOutcome: ...

    async def after(self, outcome: GateOutcome, *, ok: bool, output: Any = None, error: str | None = None) -> None: ...
