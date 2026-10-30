"""Versioned, human-owned agent identity injected into the planner, analysis and writer prompts.

The core (name, role, mission, values, style) is read-only at runtime: the self-improvement loop
can only add *lessons* and *tool stats*. ``core_hash`` lets the lesson store keep a changelog of
identity versions and notice a core edit that forgot to bump ``version``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "agent_identity.yaml"

_DEFAULT: dict[str, Any] = {
    "version": "1.0.0",
    "name": "MDRA",
    "role": "Multimodal agentic deep research assistant",
    "mission": "Answer research questions with accurate, current, source-grounded reports that a reader can verify.",
    "values": [
        "Cite a source for every factual claim; never invent citations.",
        "Prefer authoritative, primary evidence (official, academic, regulatory) over opinion or SEO pages.",
        "Flag uncertainty, conflicting evidence and gaps explicitly instead of guessing.",
    ],
    "style": "Concise, structured, plain English.",
    "learned_lessons": [],
    "changelog": [{"version": "1.0.0", "date": "", "change": "Built-in default identity."}],
}


@dataclass(frozen=True)
class AgentIdentity:
    version: str
    name: str
    role: str
    mission: str
    values: tuple[str, ...]
    style: str
    seed_lessons: tuple[str, ...] = ()
    changelog: tuple[dict[str, str], ...] = field(default_factory=tuple)
    source: str = "builtin"

    @property
    def core_hash(self) -> str:
        core = {"name": self.name, "role": self.role, "mission": self.mission,
                "values": list(self.values), "style": self.style}
        return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]

    def preamble(self, lessons: Sequence[str] = ()) -> str:
        """System-prompt block: identity core first, then (optional) learned lessons."""
        lines = [f"You are {self.name}, a {self.role}. Mission: {self.mission}", "Principles:"]
        lines += [f"- {v}" for v in self.values]
        lines.append(f"Style: {self.style}")
        learned = [*self.seed_lessons, *lessons]
        if learned:
            lines.append("Lessons learned from past runs (apply when relevant):")
            lines += [f"- {text}" for text in learned]
        return "\n".join(lines) + "\n\n"


def _clean_list(value: Any) -> tuple[str, ...]:
    return tuple(str(v).strip() for v in value if str(v).strip()) if isinstance(value, list) else ()


def load_identity(path: str | Path | None = None) -> AgentIdentity:
    """Load ``config/agent_identity.yaml`` (or ``DEEP_RESEARCH_IDENTITY_FILE``); built-in default otherwise."""
    target = Path(path or os.environ.get("DEEP_RESEARCH_IDENTITY_FILE") or DEFAULT_PATH)
    data: dict[str, Any] = dict(_DEFAULT)
    source = "builtin"
    if target.is_file():
        try:
            import yaml  # type: ignore[import-untyped]

            loaded = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data.update({k: v for k, v in loaded.items() if v not in (None, "")})
                source = str(target)
        except Exception:  # missing PyYAML or a broken file must never break a run
            source = "builtin"
    changelog = tuple({k: str(v) for k, v in c.items()} for c in data.get("changelog") or [] if isinstance(c, dict))
    return AgentIdentity(
        version=str(data["version"]), name=str(data["name"]).strip(), role=str(data["role"]).strip(),
        mission=" ".join(str(data["mission"]).split()), values=_clean_list(data["values"]) or _clean_list(_DEFAULT["values"]),
        style=str(data["style"]).strip(), seed_lessons=_clean_list(data.get("learned_lessons")),
        changelog=changelog, source=source,
    )
