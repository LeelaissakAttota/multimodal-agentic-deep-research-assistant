"""``MDRA_*`` settings for the optional scale backends, plus an honest status report."""

from __future__ import annotations

import importlib.util
import os
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
    if probe:
        _probe(rows[0], lambda: queue.ping(s))
    return rows
