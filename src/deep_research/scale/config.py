"""``MDRA_*`` settings for the optional scale backends, plus an honest status report."""

from __future__ import annotations

import importlib.util
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Any scheme; userinfo may be "user:pw", ":pw" (e.g. redis://:pw@host) or "user". Greedy up to the last "@"
# of the authority so passwords containing "@" or ":" are removed too.
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")


def redact_url_credentials(text: str) -> str:
    """Replace the userinfo of every URL in ``text`` with ``[REDACTED]``."""
    return _URL_CREDENTIALS.sub(r"\1[REDACTED]@", text)


State = Literal["off", "missing-dependency", "configured", "ok", "unreachable", "invalid"]


class ScaleSettings(BaseSettings):
    """All fields empty = every backend off (SQLite, local files, in-process runs)."""

    # 1) Background job queue (RQ on Redis)
    redis_url: SecretStr | None = Field(default=None, description="redis://host:6379/0 enables the RQ job queue")
    rq_queue: str = Field(default="mdra", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    job_timeout_seconds: int = Field(default=1800, ge=30, le=86_400)
    job_poll_seconds: float = Field(default=1.0, gt=0, le=30)

    # 2) Session storage (SQLAlchemy; postgresql://user:pass@host:5432/db). Unset = SQLite file.
    database_url: SecretStr | None = Field(default=None)

    # 3) Artifact/object storage for saved exports. Unset bucket = local folder.
    s3_bucket: str = Field(default="", max_length=63)
    s3_endpoint: str = Field(default="", max_length=300, description="e.g. http://localhost:9000 for MinIO; empty = AWS")
    s3_region: str = Field(default="us-east-1", max_length=40)
    s3_prefix: str = Field(default="mdra", max_length=100)
    s3_access_key: SecretStr | None = Field(default=None)
    s3_secret_key: SecretStr | None = Field(default=None)
    artifacts_dir: str = Field(default=str(Path("data") / "artifacts"))

    # 4) OpenTelemetry tracing: off | console | otlp (OTLP/HTTP, e.g. the compose collector on :4318)
    otel_exporter: Literal["off", "console", "otlp"] = Field(default="off")
    otel_endpoint: str = Field(default="http://localhost:4318/v1/traces", max_length=300)
    otel_service_name: str = Field(default="mdra", max_length=64)
    # Local span store for the dashboard's per-run trace timeline: off | sqlite | jsonl (works with or
    # without an exporter above; needs the tracing extra).
    trace_store: Literal["off", "sqlite", "jsonl"] = Field(default="off")
    trace_dir: str = Field(default=str(Path("data") / "traces"), max_length=500)
    # A/B experiments: inline JSON or a path to a JSON file (see deep_research.scale.experiments).
    ab_config: str = Field(default="", max_length=20_000)
    # Optional Neo4j sync for the knowledge graph (the local graph needs no service).
    neo4j_uri: str = Field(default="", max_length=500)
    neo4j_user: str = Field(default="neo4j", max_length=100)
    neo4j_password: SecretStr | None = Field(default=None)

    model_config = SettingsConfigDict(
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        env_prefix="MDRA_",
        case_sensitive=False,
        extra="ignore",
    )

    def __repr_args__(self) -> Iterable[tuple[str | None, Any]]:
        # Plain-string URL fields (neo4j_uri, otel_endpoint, s3_endpoint, ...) may carry user:pw@ - never show it.
        for key, value in super().__repr_args__():
            yield key, redact_url_credentials(value) if isinstance(value, str) else value

    def secret(self, name: str) -> str:
        value = getattr(self, name)
        return value.get_secret_value().strip() if isinstance(value, SecretStr) else ""


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _entry(name: str, configured: bool, deps: tuple[str, ...], extra: str, fallback: str, target: str = "") -> dict[str, Any]:
    missing = [d for d in deps if not has_module(d)]
    state: State = "off" if not configured else ("missing-dependency" if missing else "configured")
    detail = (f"not configured, using {fallback}" if not configured
              else f"install the '{extra}' extra (missing: {', '.join(missing)})" if missing else target)
    return {"name": name, "state": state, "configured": configured, "detail": detail, "fallback": fallback}


def _probe(entry: dict[str, Any], check: Any) -> None:
    """Upgrade a 'configured' backend to ok/unreachable. Errors are reported by type only (no URLs or secrets)."""
    if entry["state"] != "configured":
        return
    try:
        check()
    except Exception as exc:  # pragma: no cover - depends on the service
        entry.update(state="unreachable", detail=f"cannot reach the service ({type(exc).__name__}); using {entry['fallback']}")
    else:
        entry["state"] = "ok"


def backend_status(scale: ScaleSettings | None = None, *, probe: bool = False) -> list[dict[str, Any]]:
    """One row per backend. Never contains URLs with credentials, keys or passwords."""
    from deep_research.scale import queue

    s = scale or ScaleSettings()
    rows = [_entry("Job queue (RQ/Redis)", bool(s.secret("redis_url")), ("rq", "redis"), "queue",
                   "in-process runs", f"queue '{s.rq_queue}'")]
    db_url = s.secret("database_url")
    is_pg = db_url.startswith(("postgres://", "postgresql"))
    rows.append(_entry("Session database", bool(db_url), ("sqlalchemy", "psycopg") if is_pg else ("sqlalchemy",),
                       "postgres", "SQLite file (data/research_memory.sqlite)", _db_label(db_url)))
    rows.append(_entry("Artifact storage (S3/MinIO)", bool(s.s3_bucket), ("boto3",), "s3",
                       f"local folder ({s.artifacts_dir})", f"bucket '{s.s3_bucket}'"))
    otel_deps = ("opentelemetry.sdk",) + (("opentelemetry.exporter.otlp",) if s.otel_exporter == "otlp" else ())
    export = ("" if s.otel_exporter == "off" else "console exporter" if s.otel_exporter == "console"
              else f"OTLP to {_host(s.otel_endpoint)}")
    local = f"{s.trace_store} span store" if s.trace_store != "off" else ""
    rows.append(_entry("Tracing (OpenTelemetry)", s.otel_exporter != "off" or s.trace_store != "off", otel_deps, "tracing",
                       "no tracing", " + ".join(x for x in (export, local) if x)))
    rows.append(_experiment_row(s))
    rows.append(_entry("Knowledge graph (Neo4j)", bool(s.neo4j_uri), ("neo4j",), "graph",
                       "local graph on the Knowledge Base page", f"Neo4j at {_host(s.neo4j_uri)}"))
    if probe:
        _probe(rows[0], lambda: queue.ping(s))
        _probe(rows[1], lambda: _db_ping(db_url))
        _probe(rows[2], lambda: _s3_ping(s))
        _probe(rows[-1], lambda: _neo4j_ping())
    return rows


def _experiment_row(s: ScaleSettings) -> dict[str, Any]:
    row = _entry("A/B experiments", bool(s.ab_config.strip()), (), "", "default prompts and settings")
    if row["configured"]:
        from deep_research.scale.experiments import load_experiment

        try:
            exp = load_experiment(s.ab_config)
        except ValueError as exc:
            row.update(state="invalid", detail=f"invalid experiment config: {str(exc)[:160]}")
        else:
            row["detail"] = f"experiment '{exp.name}': " + ", ".join(f"{v.name} ({v.weight:g})" for v in exp.variants)
    return row


def _db_label(url: str) -> str:
    if not url:
        return ""
    try:
        from deep_research.persistence.sqlalchemy_repository import safe_url

        return safe_url(url)
    except ImportError:
        return url.split("://", 1)[0]


def _db_ping(url: str) -> None:
    import sqlalchemy as sa

    from deep_research.persistence.sqlalchemy_repository import normalize_database_url

    engine = sa.create_engine(normalize_database_url(url), connect_args={"connect_timeout": 2} if "postgres" in url else {})
    try:
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    finally:
        engine.dispose()


def _neo4j_ping() -> None:
    from deep_research.scale.graph import neo4j_driver

    driver = neo4j_driver()
    try:
        driver.verify_connectivity()
    finally:
        driver.close()


def _s3_ping(scale: ScaleSettings) -> None:
    from deep_research.scale.storage import S3ArtifactStore

    S3ArtifactStore.from_settings(scale).ping()


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname or ''}{':' + str(parts.port) if parts.port else ''}"
    except ValueError:
        return "<invalid endpoint>"
