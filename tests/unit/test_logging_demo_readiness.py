"""Demo-readiness logging: text format, key redaction, safe 422, HTTP/crash context."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest
import structlog

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from deep_research.api.main import app
from deep_research.control.api import build_control_app
from deep_research.control.service import DeepResearchControl
from deep_research.core.config import Settings
from deep_research.observability.logging import configure_logging
from deep_research.observability.redaction import exception_where, redact_event, redact_text

FAKE_KEYS = {
    "openrouter": "sk-or-v1-" + "0123456789abcdef0123",
    "nvidia": "nvapi-" + "AbCdEf0123456789xyz",
    "hf": "hf_" + "AbCdEf0123456789xyz",
    "tavily": "tvly-" + "AbCdEf0123456789xyz",
}
TOKEN = "t" * 32
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.parametrize("name", sorted(FAKE_KEYS))
def test_provider_keys_are_redacted_in_text(name: str) -> None:
    out = redact_text(f"calling provider with {FAKE_KEYS[name]} now")
    assert FAKE_KEYS[name] not in out
    assert "[REDACTED]" in out


@pytest.mark.parametrize(
    "raw",
    ["Authorization: Bearer abcDEF123456.xyz", "url?key=SECRET123&q=1", "api_key=SECRET123"],
)
def test_bearer_and_key_values_are_redacted(raw: str) -> None:
    out = redact_text(raw)
    assert "SECRET123" not in out and "abcDEF123456" not in out


def test_plain_text_is_untouched() -> None:
    assert redact_text("research_finished status=completed") == "research_finished status=completed"


def test_structlog_processor_redacts_fields_and_event() -> None:
    event = redact_event(
        None,
        "info",
        {"event": f"boom {FAKE_KEYS['openrouter']}", "openrouter_api_key": "x" * 20, "n": 3},
    )
    assert FAKE_KEYS["openrouter"] not in event["event"]
    assert event["openrouter_api_key"] == "[REDACTED]"
    assert event["n"] == 3


def test_text_format_renders_one_line(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("text")
    structlog.get_logger("demo").info("research_submitted", objective_chars=12)
    out = capsys.readouterr().out.strip().splitlines()[-1]
    assert "event='research_submitted'" in out and "objective_chars=12" in out
    configure_logging("json")
    structlog.get_logger("demo").info("research_submitted", objective_chars=12)
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert json.loads(line)["event"] == "research_submitted"


def test_log_format_setting() -> None:
    assert Settings().log_format in {"json", "text"}
    assert Settings(log_format="text").log_format == "text"


def test_exception_where_names_file_function_line() -> None:
    try:
        raise ValueError("x")
    except ValueError as exc:
        where = exception_where(exc.__traceback__)
    assert where is not None and where.startswith("test_logging_demo_readiness.py:")


def test_422_does_not_echo_submitted_value() -> None:
    client = TestClient(app)
    secret = "SECRET-INPUT-" + "9" * 10
    response = client.post("/research", json={"objective": "", "unexpected": secret})
    assert response.status_code == 422
    assert secret not in response.text
    body: dict[str, Any] = response.json()
    assert all("input" not in item for item in body["detail"])
    assert any(item["loc"][-1] in {"objective", "unexpected"} for item in body["detail"])


def test_unknown_session_returns_404() -> None:
    response = TestClient(app).get("/research/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 404


def test_control_api_422_does_not_echo_key_value(tmp_path: Path) -> None:
    control = DeepResearchControl(Settings(research_mode="offline"), config_dir=tmp_path)
    capp = build_control_app(control.manager, control.secrets, admin_token=TOKEN, title="t")
    client = TestClient(capp)
    too_long = "sk-or-v1-" + "a" * 5000
    response = client.put("/control/keys/OPENROUTER_API_KEY", json={"value": too_long}, headers=H)
    assert response.status_code == 422
    assert too_long not in response.text
    assert client.get("/control/runs", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_control_run_logs_are_redacted(tmp_path: Path) -> None:
    from deep_research.control.runtime import ControlRun

    run: Any = ControlRun.__new__(ControlRun)
    import threading

    run._cond = threading.Condition()
    run.logs = []
    ControlRun.log(run, f"provider said {FAKE_KEYS['nvidia']}", detail="Bearer abcdef123456")
    assert FAKE_KEYS["nvidia"] not in run.logs[0]["message"]
    assert "abcdef123456" not in run.logs[0]["detail"]
