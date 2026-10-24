"""Regression tests for the final-polish audit fixes."""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest

from deep_research.core.agents.report_agent import report_title
from deep_research.domain.research.research_state import ResearchState

ROOT = Path(__file__).resolve().parents[2]


def test_report_title_uses_objective() -> None:
    state = ResearchState(request_id=uuid4(), metadata={"objective": "  What is   ZetaCell? "})
    assert report_title(state) == "Research Report: What is ZetaCell?"


def test_report_title_truncates_long_objective() -> None:
    state = ResearchState(request_id=uuid4(), metadata={"objective": "x" * 500})
    title = report_title(state)
    assert len(title) <= len("Research Report: ") + 120
    assert title.endswith("\u2026")


def test_report_title_falls_back_to_request_id() -> None:
    rid = uuid4()
    assert report_title(ResearchState(request_id=rid)) == f"Research Report for Request {rid}"


def test_api_report_title_contains_objective() -> None:
    from fastapi.testclient import TestClient

    from deep_research.api.main import app

    client = TestClient(app)
    resp = client.post("/research", json={"objective": "Summarise solid-state battery progress"})
    assert resp.status_code in (200, 201, 202)
    body = resp.json()
    if body.get("report"):
        assert "Summarise solid-state battery progress" in body["report"]["title"]


def test_env_example_documents_control_token() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "DEEP_RESEARCH_CONTROL_TOKEN=" in text


def test_license_names_owner() -> None:
    assert "Attota Leelaissak" in (ROOT / "LICENSE").read_text(encoding="utf-8")


def test_control_server_binds_localhost_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_research.control import server

    captured: dict = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: captured.update(kw, app=app))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_TOKEN", "t" * 32)
    monkeypatch.setattr(sys, "argv", ["deep-research-control", "--port", "8799"])
    server.main()
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8799
    assert captured["app"] is not None


def test_control_server_default_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from deep_research.control import server

    captured: dict = {}
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kw: captured.update(kw))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_TOKEN", "t" * 32)
    monkeypatch.setattr(sys, "argv", ["deep-research-control"])
    server.main()
    assert captured["port"] == 8711


def test_internal_notes_not_in_repo_root() -> None:
    for name in ("REPORT.md", "PROJECT_STATUS.md", "TASKS.md", "PHASE7_ACCEPTANCE_AUDIT.md", "RESUME.md"):
        assert not (ROOT / name).exists(), name


def test_readme_test_count_matches_suite() -> None:
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    m = re.search(r"(\d+) tests", readme)
    assert m, "README should state the test count"
    count = sum(
        len(re.findall(r"^\s*(?:async )?def test_", p.read_text(encoding="utf-8"), re.M))
        for p in (ROOT / "tests").rglob("test_*.py")
    )
    assert int(m.group(1)) >= count
