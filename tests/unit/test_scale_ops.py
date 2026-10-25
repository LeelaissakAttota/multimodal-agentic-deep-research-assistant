"""docker-compose, docs and the dashboard's honest backend status panel."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = str(ROOT / "src/deep_research/dashboard/app.py")


def test_compose_services_are_local_only() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for service in ("redis:", "postgres:", "minio:", "otel-collector:", "neo4j:"):
        assert f"\n  {service}" in text
    ports = re.findall(r'"(\d[\d.:]*:\d+)"', text)
    assert ports and all(p.startswith("127.0.0.1:") for p in ports)
    assert (ROOT / "deploy/otel/collector.yaml").is_file() and "4318" in (ROOT / "deploy/otel/collector.yaml").read_text(encoding="utf-8")
    yaml = pytest.importorskip("yaml")
    assert set(yaml.safe_load(text)["services"]) >= {"redis", "postgres", "minio", "otel-collector", "neo4j"}


def test_docs_cover_every_backend() -> None:
    doc = (ROOT / "docs/SCALE.md").read_text(encoding="utf-8")
    for var in ("MDRA_REDIS_URL", "MDRA_DATABASE_URL", "MDRA_S3_BUCKET", "MDRA_OTEL_EXPORTER", "MDRA_AB_CONFIG",
                "MDRA_NEO4J_URI", "SimpleWorker", "extensions/mdra-chrome"):
        assert var in doc
    assert "docs/SCALE.md" in (ROOT / "README.md").read_text(encoding="utf-8")


def _run_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> object:
    testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("DEEP_RESEARCH_DASHBOARD_DIR", str(tmp_path))
    monkeypatch.setenv("DEEP_RESEARCH_CONTROL_URL", "http://127.0.0.1:9")
    import streamlit as st

    st.cache_data.clear()  # backend probes are cached for 60 s across app runs in this process
    at = testing.AppTest.from_file(APP, default_timeout=30)
    at.query_params["page"] = "tools"
    at.run()
    assert not at.exception
    return at


def test_dashboard_backends_default_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    at = _run_tools(tmp_path, monkeypatch)
    text = " ".join(m.value for m in at.markdown)  # type: ignore[attr-defined]
    assert "Scale backends" in text and "Job queue (RQ/Redis)" in text and text.count("not configured") >= 6
    assert not at.warning  # type: ignore[attr-defined]


def test_dashboard_marks_unavailable_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_AB_CONFIG", '{"name": "broken"}')
    monkeypatch.setenv("MDRA_REDIS_URL", "redis://:hunter2pw@127.0.0.1:9/0")
    at = _run_tools(tmp_path, monkeypatch)
    text = " ".join(m.value for m in at.markdown)  # type: ignore[attr-defined]
    assert "invalid config" in text and "hunter2pw" not in text
    warning = " ".join(w.value for w in at.warning)  # type: ignore[attr-defined]
    assert "A/B experiments" in warning and "Job queue" in warning
