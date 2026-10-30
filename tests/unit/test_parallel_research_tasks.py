"""Independent research tasks run concurrently unless a human approves each step."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from deep_research.core.orchestration.master_research_orchestrator import MasterResearchOrchestrator
from deep_research.core.orchestration.step_gate import GateOutcome
from deep_research.domain.research.research_task import ResearchTask
from deep_research.tools.tool import ToolResult


class SlowAgent:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def execute_task(self, task: ResearchTask, context: Any = None) -> ToolResult:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.2)
        self.active -= 1
        return ToolResult(success=True, output={"summary": task.objective, "evidence_ids": [], "source_ids": []},
                          tool_name="web_search")


class Gate:
    def __init__(self, parallel: bool) -> None:
        self.parallel_safe = parallel
        self.after_calls = 0

    async def before(self, kind: str, name: str, inputs: dict[str, Any]) -> GateOutcome:
        return GateOutcome(approved=True)

    async def after(self, outcome: GateOutcome, *, ok: bool, output: Any = None, error: str | None = None) -> None:
        self.after_calls += 1


def tasks() -> list[ResearchTask]:
    return [ResearchTask(id=UUID(int=i + 1), plan_id=UUID(int=99), description=f"t{i}", objective=f"o{i}") for i in range(3)]


def orchestrator(agent: SlowAgent, gate: Gate | None) -> MasterResearchOrchestrator:
    return MasterResearchOrchestrator(MagicMock(), agent, MagicMock(), MagicMock(), MagicMock(), step_gate=gate)  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", [None, Gate(True)])
async def test_tasks_run_concurrently_without_step_approval(gate: Gate | None) -> None:
    agent = SlowAgent()
    started = time.perf_counter()
    done = await orchestrator(agent, gate)._execute_research_tasks(tasks())
    assert time.perf_counter() - started < 0.5
    assert agent.peak == 3
    assert [t.status for t in done] == ["completed"] * 3 and [t.result for t in done] == ["o0", "o1", "o2"]
    if gate is not None:
        assert gate.after_calls == 3


@pytest.mark.asyncio
async def test_tasks_run_one_by_one_with_step_approval() -> None:
    agent = SlowAgent()
    done = await orchestrator(agent, Gate(False))._execute_research_tasks(tasks())
    assert agent.peak == 1 and all(t.status == "completed" for t in done)
