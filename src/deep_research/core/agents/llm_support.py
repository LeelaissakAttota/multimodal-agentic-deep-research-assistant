"""Shared helpers for the LLM-driven agents: bounded calls, JSON parsing, decision log.

Every LLM decision is *advisory and validated*: callers parse the reply with
``parse_json_object`` and validate it themselves; any failure (rate limit,
provider error, timeout, invalid JSON, schema violation) makes the caller fall
back to its deterministic counterpart for that single decision. Each decision
is recorded in ``state.metadata["agent_decisions"]`` with its ``source``
(``llm`` or ``deterministic``) and, for fallbacks, the reason.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from deep_research.core.free_model_guard import ModelCapability
from deep_research.domain.research.research_state import ResearchState
from deep_research.errors.base import ModelError
from deep_research.models.model_gateway import ModelGateway, ModelRequest

DECISIONS_KEY = "agent_decisions"
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class AgentDecisionError(Exception):
    """An LLM decision could not be used; ``reason`` is a short machine-readable code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


async def ask_json(
    gateway: ModelGateway,
    prompt: str,
    *,
    max_tokens: int,
    timeout_seconds: float,
) -> tuple[dict[str, Any], str]:
    """One bounded LLM call that must return a JSON object. Raises ``AgentDecisionError``."""
    request = ModelRequest(
        prompt=prompt,
        parameters={"max_tokens": max_tokens, "required_capability": ModelCapability.TEXT_GENERATION.value},
    )
    try:
        response = await asyncio.wait_for(gateway.generate(request), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise AgentDecisionError("timeout", f"no reply within {timeout_seconds:g}s") from exc
    except ModelError as exc:
        code = exc.error_code or "model_error"
        raise AgentDecisionError("rate_limit" if code == "provider_rate_limit" else code, str(exc)[:200]) from exc
    except Exception as exc:  # provider bugs must never kill the run
        raise AgentDecisionError("model_error", type(exc).__name__) from exc
    return parse_json_object(response.text), response.model_id


def parse_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from model text (handles fences and <think> blocks)."""
    cleaned = _THINK.sub("", text or "").strip()
    fenced = _FENCE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    start = cleaned.find("{")
    if start < 0:
        raise AgentDecisionError("invalid_json", "no JSON object in reply")
    try:
        value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise AgentDecisionError("invalid_json", exc.msg) from exc
    if not isinstance(value, dict):
        raise AgentDecisionError("invalid_json", "reply is not a JSON object")
    return value


def clean_text(value: Any, *, max_len: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_len]


def record_decision(state: ResearchState, entry: dict[str, Any]) -> None:
    """Append one labelled decision to the run's decision log."""
    log = state.metadata.setdefault(DECISIONS_KEY, [])
    if isinstance(log, list):
        log.append({"iteration": state.iteration_number, **entry})
