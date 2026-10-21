"""Bearer-protected FastAPI control API (bind to 127.0.0.1 only)."""

from __future__ import annotations

import hmac
import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from deep_research.api.error_handlers import install_error_handlers

from .runtime import RunManager
from .secrets_store import SecretStore, mask


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict[str, Any] = Field(default_factory=dict)


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=512)
    inputs: dict[str, Any] | None = None


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_mode: bool | None = None
    auto_approve_low_risk: bool | None = None


class KeyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(max_length=4096)


def _operator(x_operator: str | None) -> str:
    name = " ".join((x_operator or "dashboard").split())[:64]
    return name or "dashboard"


def build_control_app(  # noqa: C901 - flat route table
    manager: RunManager,
    secrets: SecretStore,
    *,
    admin_token: str,
    title: str,
) -> FastAPI:
    if len(admin_token) < 16:
        raise ValueError("control admin token must be at least 16 characters")
    expected = admin_token.encode()

    def require_token(authorization: str | None = Header(default=None)) -> None:
        supplied = (authorization or "").removeprefix("Bearer ").strip().encode()
        if not authorization or not authorization.startswith("Bearer ") or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    app = FastAPI(title=f"{title} control API", docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)
    auth = [Depends(require_token)]

    def run_or_404(run_id: str) -> Any:
        try:
            return manager.get(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found") from None

    @app.get("/control/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": title}

    @app.get("/control/settings", dependencies=auth)
    def get_settings() -> dict[str, bool]:
        return manager.settings.load()

    @app.put("/control/settings", dependencies=auth)
    def put_settings(body: SettingsBody, x_operator: str | None = Header(default=None)) -> dict[str, bool]:
        updated = manager.settings.update(
            approval_mode=body.approval_mode, auto_approve_low_risk=body.auto_approve_low_risk
        )
        manager.audit.record({"event": "settings_changed", "by": _operator(x_operator), **updated})
        return updated

    @app.post("/control/runs", dependencies=auth, status_code=201)
    def create_run(body: RunBody) -> dict[str, Any]:
        return manager.create(body.params).to_dict()

    @app.get("/control/runs", dependencies=auth)
    def list_runs() -> list[dict[str, Any]]:
        return [r.to_dict(include_steps=False) for r in manager.list()]

    @app.get("/control/runs/{run_id}", dependencies=auth)
    def get_run(run_id: str) -> dict[str, Any]:
        return dict(run_or_404(run_id).to_dict())

    @app.get("/control/runs/{run_id}/steps", dependencies=auth)
    def get_steps(run_id: str) -> list[dict[str, Any]]:
        return [s.to_dict() for s in run_or_404(run_id).steps]

    def _lifecycle(run_id: str, action: str) -> dict[str, Any]:
        run = run_or_404(run_id)
        try:
            getattr(run, action)()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        manager.audit.record({"event": f"run_{action}", "run_id": run_id})
        return dict(run.to_dict(include_steps=False))

    @app.post("/control/runs/{run_id}/pause", dependencies=auth)
    def pause(run_id: str) -> dict[str, Any]:
        return _lifecycle(run_id, "pause")

    @app.post("/control/runs/{run_id}/resume", dependencies=auth)
    def resume(run_id: str) -> dict[str, Any]:
        return _lifecycle(run_id, "resume")

    @app.post("/control/runs/{run_id}/stop", dependencies=auth)
    def stop(run_id: str) -> dict[str, Any]:
        return _lifecycle(run_id, "stop")

    def _decide(run_id: str, step_id: str, action: str, body: DecisionBody, who: str) -> dict[str, Any]:
        run = run_or_404(run_id)
        try:
            if action == "approve":
                step = run.approve(step_id, by=who, note=body.note, inputs=body.inputs)
            else:
                step = run.reject(step_id, by=who, note=body.note)
        except KeyError:
            raise HTTPException(status_code=404, detail="step not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return dict(step.to_dict())

    @app.post("/control/runs/{run_id}/steps/{step_id}/approve", dependencies=auth)
    def approve(run_id: str, step_id: str, body: DecisionBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        return _decide(run_id, step_id, "approve", body, _operator(x_operator))

    @app.post("/control/runs/{run_id}/steps/{step_id}/reject", dependencies=auth)
    def reject(run_id: str, step_id: str, body: DecisionBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        return _decide(run_id, step_id, "reject", body, _operator(x_operator))

    @app.post("/control/runs/{run_id}/steps/{step_id}/retry", dependencies=auth)
    def retry(run_id: str, step_id: str, body: DecisionBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        run_or_404(run_id)
        try:
            step = manager.retry(run_id, step_id, by=_operator(x_operator), inputs=body.inputs)
        except KeyError:
            raise HTTPException(status_code=404, detail="step not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return dict(step.to_dict())

    @app.get("/control/runs/{run_id}/logs", dependencies=auth)
    def logs(run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
        run = run_or_404(run_id)
        items = [x for x in run.logs if x["seq"] >= after]
        return {"run_id": run_id, "status": run.status.value, "logs": items}

    @app.get("/control/runs/{run_id}/logs/stream", dependencies=auth)
    def log_stream(run_id: str, after: int = Query(default=0, ge=0), max_seconds: float = Query(default=30, le=300)) -> StreamingResponse:
        run = run_or_404(run_id)

        def events() -> Iterator[str]:
            cursor, deadline = after, time.monotonic() + max_seconds
            while time.monotonic() < deadline:
                items = run.wait_for_logs(cursor, timeout=1.0)
                for item in items:
                    cursor = item["seq"] + 1
                    yield f"data: {json.dumps(item, default=str)}\n\n"
                if run.finished_at and not items:
                    yield "event: end\ndata: {}\n\n"
                    return

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/control/approvals", dependencies=auth)
    def approvals(run_id: str | None = None, limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [e for e in manager.audit.tail(limit * 3, run_id) if e.get("event") == "step_decision"][:limit]

    @app.get("/control/audit", dependencies=auth)
    def audit(limit: int = Query(default=200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return manager.audit.tail(limit)

    @app.get("/control/keys", dependencies=auth)
    def keys() -> dict[str, Any]:
        return {"store": "local-file", "keys": secrets.masked()}

    @app.put("/control/keys/{name}", dependencies=auth)
    def put_key(name: str, body: KeyBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            secrets.set(name, body.value)
        except KeyError:
            raise HTTPException(status_code=404, detail="unknown key name") from None
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid key value") from None
        manager.audit.record({"event": "key_changed", "name": name, "by": _operator(x_operator),
                              "cleared": not body.value.strip()})
        return {"name": name, "configured": bool(body.value.strip()), "masked": mask(body.value.strip())}

    return app
