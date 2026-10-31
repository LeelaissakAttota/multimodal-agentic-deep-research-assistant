"""A/B experiments for prompt / planner variants (off unless ``MDRA_AB_CONFIG`` is set).

Config (inline JSON or a path to a JSON file)::

    {"name": "planner-style", "variants": [
        {"name": "control", "weight": 1},
        {"name": "broad", "weight": 1, "overrides": {"agent_plan_style": "broad", "agent_max_plan_tasks": 5}}]}

Each run is assigned a variant deterministically from ``sha256(experiment:run_id)``, so the same run id
always lands in the same variant (also on a queue worker). Overrides are limited to the whitelisted
research settings below and validated by :class:`Settings`; the variant is recorded in the run result
so ``deep-research-eval --by-variant`` can compare variants.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from deep_research.core.config import Settings

ALLOWED_OVERRIDES = frozenset({
    "agent_plan_style", "agent_max_plan_tasks", "agent_tool_steps", "agent_mode",
    "max_research_iterations", "live_max_search_results", "live_max_fetch_pages", "openrouter_model",
})


@dataclass(frozen=True)
class Variant:
    name: str
    weight: float = 1.0
    overrides: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Experiment:
    name: str
    variants: tuple[Variant, ...]

    def assign(self, run_id: str) -> Variant:
        """Deterministic weighted choice for ``run_id``."""
        digest = hashlib.sha256(f"{self.name}:{run_id}".encode()).digest()
        point = int.from_bytes(digest[:8], "big") / 2**64 * sum(v.weight for v in self.variants)
        for variant in self.variants:
            point -= variant.weight
            if point < 0:
                return variant
        return self.variants[-1]


def _validate_overrides(name: str, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"variant '{name}': overrides must be an object")
    unknown = sorted(set(raw) - ALLOWED_OVERRIDES)
    if unknown:
        raise ValueError(f"variant '{name}': overrides not allowed: {', '.join(unknown)}")
    probe = Settings.model_construct()
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        try:
            Settings.__pydantic_validator__.validate_assignment(probe, key, value)
        except Exception as exc:
            raise ValueError(f"variant '{name}': bad value for {key}") from exc
        clean[key] = getattr(probe, key)
    return clean


def parse_experiment(data: Any) -> Experiment:
    if not isinstance(data, dict):
        raise ValueError("experiment config must be a JSON object")
    name = str(data.get("name") or "").strip()
    if not name or len(name) > 100:
        raise ValueError("experiment needs a name (1-100 characters)")
    raw_variants = data.get("variants")
    if not isinstance(raw_variants, list) or len(raw_variants) < 2:
        raise ValueError("experiment needs at least two variants")
    variants: list[Variant] = []
    for raw in raw_variants:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            raise ValueError("every variant needs a name")
        vname = str(raw["name"]).strip()[:100]
        weight = raw.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 1000:
            raise ValueError(f"variant '{vname}': weight must be a number in (0, 1000]")
        variants.append(Variant(vname, float(weight), _validate_overrides(vname, raw.get("overrides"))))
    if len({v.name for v in variants}) != len(variants):
        raise ValueError("variant names must be unique")
    return Experiment(name, tuple(variants))


def load_experiment(source: str) -> Experiment:
    """Parse ``source`` as inline JSON (starts with ``{``) or a path to a JSON file."""
    text = source.strip()
    if not text.startswith("{"):
        path = Path(text).expanduser()
        if not path.is_file():
            raise ValueError("experiment config file not found")
        text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"experiment config is not valid JSON (line {exc.lineno})") from exc
    return parse_experiment(data)


def active_experiment(source: str | None = None) -> Experiment | None:
    """The configured experiment, or ``None`` when ``MDRA_AB_CONFIG`` is unset. Invalid config raises ValueError."""
    if source is None:
        from deep_research.scale.config import ScaleSettings

        source = ScaleSettings().ab_config
    return load_experiment(source) if source.strip() else None


def apply_variant(settings: Settings, run_id: str, experiment: Experiment | None) -> tuple[Settings, dict[str, Any] | None]:
    """Return settings with the run's variant applied plus the assignment record (``None`` when no experiment)."""
    if experiment is None:
        return settings, None
    variant = experiment.assign(run_id)
    record = {"experiment": experiment.name, "variant": variant.name, "overrides": dict(variant.overrides)}
    return settings.model_copy(update=variant.overrides), record
