"""OpenTelemetry tracing: off by default, spans for run/agents/tools, and no secrets in spans."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr

pytest.importorskip("opentelemetry.sdk")

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.config import Settings
from deep_research.scale import backend_status
from deep_research.scale.tracing import configure_tracing, reset_tracing, safe_attributes, span, tracing_enabled

SECRETS = ("sk-or-v1-abcdef1234567890", "tvly-SECRETVALUE123", "hunter2pw", "Bearer abc.def.ghi")


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    reset_tracing()
    yield
    reset_tracing()


def test_off_by_default() -> None:
    assert not tracing_enabled()
    with span("x", a=1) as s:
        s.set("b", 2)  # no-op
    assert backend_status()[3]["state"] == "off"


def test_console_exporter_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_OTEL_EXPORTER", "console")
    assert tracing_enabled()
    assert backend_status()[3]["state"] == "configured"


def test_otlp_status_hides_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_OTEL_EXPORTER", "otlp")
    monkeypatch.setenv("MDRA_OTEL_ENDPOINT", "http://user:hunter2pw@collector:4318/v1/traces")
    row = backend_status()[3]
    assert "collector:4318" in row["detail"] and "hunter2pw" not in json.dumps(row)


def test_safe_attributes() -> None:
    attrs = safe_attributes({
        "api_key": "x", "authorization": "y", "db_password": "z", "mdra.token_count": 3,
        "url": "https://bob:hunter2pw@example.org/p?token=abc123", "note": "key=sk-or-v1-abcdef1234567890 Bearer abc.def.ghi",
        "ok": True, "n": 2.5, "list": ["tvly-SECRETVALUE123", 1], "none": None, "long": "a" * 1000,
    })
    assert set(attrs) == {"url", "note", "ok", "n", "list", "long"}
    text = json.dumps(attrs)
    assert not any(s in text for s in SECRETS) and "abc123" not in text and len(attrs["long"]) == 256


def test_run_agent_tool_spans_without_secrets() -> None:
    exporter = InMemorySpanExporter()
    assert configure_tracing(exporter=exporter, force=True)
    settings = Settings(_env_file=None, research_mode="offline", openrouter_api_key=SecretStr(SECRETS[0]),
                        tavily_api_key=SecretStr(SECRETS[1]))
    objective = f"What are the health benefits of green tea? api_key={SECRETS[1]} password {SECRETS[2]}"
    response = asyncio.run(ResearchApplication(settings).submit(ResearchSubmission(objective=objective)))
    assert response.status == "completed"
    spans = exporter.get_finished_spans()
    names = [s.name for s in spans]
    assert "mdra.run" in names and "mdra.tool" in names
    agents = {s.attributes.get("mdra.agent") for s in spans if s.name == "mdra.agent"}
    assert {"planning", "research", "analysis", "evaluation", "reporting"} <= agents
    run = next(s for s in spans if s.name == "mdra.run")
    assert run.attributes["mdra.mode"] == "offline" and run.attributes["mdra.status"] == "completed"
    tool = next(s for s in spans if s.name == "mdra.tool")
    assert tool.attributes["mdra.tool"] and isinstance(tool.attributes["mdra.tool.params"], tuple)
    dump = json.dumps([{"n": s.name, "a": dict(s.attributes or {}), "e": [dict(e.attributes or {}) for e in s.events],
                        "r": dict(s.resource.attributes)} for s in spans], default=str)
    assert not any(secret in dump for secret in SECRETS)
    assert "green tea" not in dump  # objective/prompt text is never attached


def test_exception_recorded_by_type_only() -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(exporter=exporter, force=True)
    with pytest.raises(ValueError):
        with span("boom"):
            raise ValueError(f"leak {SECRETS[0]}")
    finished: Any = exporter.get_finished_spans()[0]
    assert finished.attributes["error.type"] == "ValueError" and SECRETS[0] not in str(finished.to_json())


def test_redis_style_url_password_never_in_spans() -> None:
    exporter = InMemorySpanExporter()
    configure_tracing(exporter=exporter, force=True)
    pw = "Redis-Pw-123456"
    with span("db", **{"mdra.target": f"redis://:{pw}@cache:6379/0", "mdra.user_only": f"ftp://{pw}@h/x"}):
        pass
    attrs = dict(exporter.get_finished_spans()[0].attributes or {})
    assert pw not in json.dumps(attrs) and attrs["mdra.target"] == "redis://[REDACTED]@cache:6379/0"
    assert safe_attributes({"u": "https://example.org/a@b"})["u"] == "https://example.org/a@b"  # '@' in path kept
