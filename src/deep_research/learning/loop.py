"""The self-improvement loop wired around one research run (no human in the loop).

before_run: load identity, retrieve top-k relevant lessons, rank/skip tools by reliability,
            build the identity+lessons preamble that the planner/analysis/writer prompts start with.
during run: ``ToolStatsResearchAgent`` records success and latency for every research task's tool.
after_run:  reflect (LLM with deterministic fallback) -> validated lessons; store the run score.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from deep_research.context.context_builder import AgentContext
from deep_research.core.agents.research_agent import ResearchAgent
from deep_research.domain.research.research_task import ResearchTask
from deep_research.learning.identity import AgentIdentity, load_identity
from deep_research.learning.lessons import LessonStore
from deep_research.learning.reflection import classify_topic, collect_signals, reflect
from deep_research.tools.tool import ToolResult

LEARNING_KEY = "learning"


@dataclass
class RunLearning:
    """What the loop decided for one run (also copied into ``report["learning"]``)."""

    topic: str
    applied: list[dict[str, Any]] = field(default_factory=list)
    tool_order: list[str] = field(default_factory=list)
    skipped_tools: list[str] = field(default_factory=list)
    preamble: str = ""
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


class ToolStatsResearchAgent(ResearchAgent):
    """Transparent wrapper that times each task and feeds tool reliability stats."""

    def __init__(self, inner: ResearchAgent, store: LessonStore, run: RunLearning) -> None:
        self.inner, self.store, self.run = inner, store, run

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def execute_task(self, task: ResearchTask, context: AgentContext | None = None) -> ToolResult:
        tool = str((task.metadata or {}).get("source_tool") or task.assigned_tool or "research")
        start = time.monotonic()
        error: str | None = None
        try:
            result = await (self.inner.execute_task(task, context=context) if context is not None
                            else self.inner.execute_task(task))
            success, error = bool(result.success), result.error
            return result
        except Exception as exc:
            success, error = False, type(exc).__name__
            raise
        finally:
            ms = (time.monotonic() - start) * 1000
            self.store.record_tool(tool, success, ms, error)
            self.run.tool_events.append({"tool": tool, "success": success, "latency_ms": round(ms, 1)})


class SelfImprovement:
    def __init__(self, store_path: str | Path, *, enabled: bool = True, top_k: int = 3, max_lessons: int = 50,
                 identity: AgentIdentity | None = None) -> None:
        self.enabled = enabled
        self.top_k = top_k
        self.identity = identity or load_identity()
        self.store = LessonStore(store_path, max_lessons=max_lessons) if enabled else None

    def before_run(self, objective: str, tools: Sequence[str]) -> RunLearning:
        run = RunLearning(topic=classify_topic(objective), tool_order=list(tools))
        lessons: list[str] = []
        if self.store is not None:
            self.store.note_identity(self.identity.version, self.identity.core_hash)
            found = self.store.retrieve(objective, run.topic, self.top_k)
            self.store.mark_used(x.id for x in found)
            run.applied = [{"id": x.id, "text": x.text, "confidence": round(x.confidence, 2), "topic": x.topic}
                           for x in found]
            lessons = [x.text for x in found]
            run.tool_order, run.skipped_tools = self.store.rank_tools(tools)
        run.preamble = self.identity.preamble(lessons)
        return run

    def wrap(self, agent: ResearchAgent, run: RunLearning) -> ResearchAgent:
        return ToolStatsResearchAgent(agent, self.store, run) if self.store is not None else agent

    async def after_run(self, run: RunLearning, *, run_id: str, objective: str, status: str,
                        report: dict[str, Any] | None, runtime: dict[str, Any] | None,
                        metadata: dict[str, Any] | None, gateway: Any = None,
                        timeout_seconds: float = 30.0) -> dict[str, Any]:
        summary: dict[str, Any] = {"enabled": self.enabled, "identity_version": self.identity.version,
                                   "topic": run.topic, "applied_lessons": run.applied,
                                   "tool_order": run.tool_order, "skipped_tools": run.skipped_tools}
        if self.store is None:
            return summary
        signals = collect_signals(run_id, objective, status, report, runtime, run.tool_events, metadata,
                                  elapsed_seconds=time.monotonic() - run.started)
        signals.lessons_applied = len(run.applied)
        candidates, source = await reflect(signals, gateway=gateway, preamble=run.preamble,
                                           timeout_seconds=timeout_seconds)
        created = []
        for text, confidence in candidates:
            lesson = self.store.add(text, topic=run.topic, run_id=run_id, confidence=confidence, source=source,
                                    evidence=_evidence(signals.as_dict()))
            if lesson is not None:
                created.append({"id": lesson.id, "text": lesson.text, "confidence": round(lesson.confidence, 2)})
        self.store.record_run(run_id, topic=run.topic, score=signals.score(), lessons_applied=len(run.applied),
                              metrics=signals.as_dict(), objective_chars=len(objective))
        summary.update({"score": signals.score(), "signals": signals.as_dict(), "reflection_source": source,
                        "new_lessons": created})
        return summary


def _evidence(sig: dict[str, Any]) -> str:
    parts = [f"score {sig['score']}", f"{sig['citations']} citations", f"coverage {sig['claim_coverage']}",
             f"{sig['credible_citations']} credible", f"{sig['elapsed_seconds']} s"]
    if sig["failed_tools"]:
        parts.append("failed: " + ", ".join(sig["failed_tools"]))
    return "; ".join(parts)
