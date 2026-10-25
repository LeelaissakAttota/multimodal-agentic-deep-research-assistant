"""``MDRA_*`` settings for the optional scale backends, plus an honest status report."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

State = Literal["off", "missing-dependency", "configured", "ok", "unreachable"]


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

    model_config = SettingsConfigDict(
        env_file=os.environ.get("DEEP_RESEARCH_ENV_FILE") or ".env",
        env_file_encoding="utf-8",
        env_prefix="MDRA_",
        case_sensitive=False,
        extra="ignore",
    )

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
    if probe:
        _probe(rows[0], lambda: queue.ping(s))
        _probe(rows[1], lambda: _db_ping(db_url))
        _probe(rows[2], lambda: _s3_ping(s))
    return rows


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


def _s3_ping(scale: ScaleSettings) -> None:
    from deep_research.scale.storage import S3ArtifactStore

    S3ArtifactStore.from_settings(scale).ping()
