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
    ports = [p for line in re.findall(r"ports: \[(.*)\]", text) for p in re.findall(r'"([^"]+)"', line)]
    assert len(ports) == 7 and all(p.startswith("127.0.0.1:${MDRA_") for p in ports)
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


def test_dashboard_shows_queue_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_REDIS_URL", "redis://:hunter2pw@127.0.0.1:9/0")  # closed local port -> unreachable
    at = _run_tools(tmp_path, monkeypatch)
    info = " ".join(i.value for i in at.info)  # type: ignore[attr-defined]
    assert "fallback" in info and "in-process" in info and "hunter2pw" not in info


def test_compose_host_ports_configurable_with_safe_defaults() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    expected = {"MDRA_PG_PORT": ("5433", "5432"), "MDRA_REDIS_PORT": ("6379", "6379"), "MDRA_MINIO_PORT": ("9000", "9000"),
                "MDRA_MINIO_CONSOLE_PORT": ("9001", "9001"), "MDRA_OTEL_PORT": ("4318", "4318"),
                "MDRA_NEO4J_HTTP_PORT": ("7474", "7474"), "MDRA_NEO4J_BOLT_PORT": ("7687", "7687")}
    for var, (default, container) in expected.items():
        assert f'"127.0.0.1:${{{var}:-{default}}}:{container}"' in text
    doc = (ROOT / "docs/SCALE.md").read_text(encoding="utf-8")
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert all(var in doc and var in env for var in expected)
    assert "@localhost:5433/mdra" in doc and "@localhost:5433/mdra" in env and "localhost:5432" not in doc + env


def test_compose_images_are_pinned_and_not_removed_upstream() -> None:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    images = dict(re.findall(r"\n  ([a-z0-9-]+):\n(?:    #.*\n)*    image: (\S+)", text))
    assert {"redis", "postgres", "minio", "minio-init", "otel-collector", "neo4j"} <= set(images)
    assert not any(img.split("@")[0].split(":")[0].removeprefix("docker.io/") in ("minio/minio", "minio/mc")
                   or "quay.io/minio" in img for img in images.values())
    for name in ("minio", "minio-init"):
        assert re.fullmatch(r"docker\.io/pgsty/(silo|mc):RELEASE\.[0-9TZ-]+@sha256:[0-9a-f]{64}", images[name]), images[name]
