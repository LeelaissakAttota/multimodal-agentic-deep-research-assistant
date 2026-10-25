"""Bearer-protected FastAPI control API (bind to 127.0.0.1 only)."""

from __future__ import annotations

import hmac
import json
import re
import time
from collections.abc import Iterator
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from deep_research.api.error_handlers import install_error_handlers

from .runtime import RunManager
from .secrets_store import SecretStore, mask


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    params: dict[str, Any] = Field(default_factory=dict)
    #: "Step-by-step approval" for this run only; None = use the saved setting (GET /control/settings).
    approval_mode: bool | None = None


class DecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str | None = Field(default=None, max_length=512)
    inputs: dict[str, Any] | None = None


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval_mode: bool | None = None
    auto_approve_low_risk: bool | None = None


class ShareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: dict[str, Any] | None = None  # for runs only kept by the dashboard (not in this API's memory)
    expires_in_days: float | None = Field(default=None, ge=0, le=365)  # None = server default, 0 = never


class ArtifactBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    report: dict[str, Any] | None = None  # for runs only kept by the dashboard


class ClipBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=8, max_length=2000)
    title: str = Field(default="", max_length=500)
    selection: str = Field(default="", max_length=20_000)


LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})


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
    assistant: Any | None = None,
    share_ttl_days: float = 7.0,
    artifacts: Any | None = None,
    clips: Any | None = None,
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
        return manager.create(body.params, approval_mode=body.approval_mode).to_dict()

    @app.get("/control/runs", dependencies=auth)
    def list_runs() -> list[dict[str, Any]]:
        return [r.to_dict(include_steps=False) for r in manager.list()]

    @app.get("/control/runs/{run_id}", dependencies=auth)
    def get_run(run_id: str) -> dict[str, Any]:
        return dict(run_or_404(run_id).to_dict())

    @app.get("/control/runs/{run_id}/export", dependencies=auth)
    def export(run_id: str, format: str = Query(default="pdf", pattern="^(pdf|docx|pptx|json|md|html)$")) -> Response:
        from deep_research.export import FORMATS, ExportUnavailable, export_filename, export_report

        run = run_or_404(run_id)
        result = run.result if isinstance(run.result, dict) else {}
        report = result.get("report")
        if not isinstance(report, dict):
            raise HTTPException(status_code=409, detail="run has no report yet")
        try:
            body = export_report(report, format)
        except ExportUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        name = export_filename(report, format, run_id)
        return Response(body, media_type=FORMATS[format][0], headers={"Content-Disposition": f'attachment; filename="{name}"'})

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

    if assistant is not None:
        _share_routes(app, manager, assistant, auth, share_ttl_days)
    _artifact_routes(app, manager, auth, artifacts)
    if clips is not None:
        _clip_routes(app, manager, auth, clips)
    return app


def _clip_routes(app: FastAPI, manager: RunManager, auth: list[Any], clips: Any) -> None:
    """Browser-extension inbox: pages/selections sent "as source". Loopback clients only."""

    def loopback_only(request: Request) -> None:
        if (request.client.host if request.client else "") not in LOOPBACK:
            raise HTTPException(status_code=403, detail="clips are accepted from this computer only")

    guard = [Depends(loopback_only), *auth]

    @app.post("/control/clips", dependencies=guard, status_code=201)
    def add_clip(body: ClipBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            item: dict[str, Any] = clips.add(body.url, body.title, body.selection, by=_operator(x_operator))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
        manager.audit.record({"event": "source_clipped", "clip": item["id"], "by": item["by"]})
        return item

    @app.get("/control/clips", dependencies=guard)
    def list_clips(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return list(clips.list(limit))


def _artifact_routes(app: FastAPI, manager: RunManager, auth: list[Any], store: Any | None) -> None:  # noqa: C901 - flat route table
    """Save exports to the artifact store (local folder by default, S3/MinIO via MDRA_S3_*)."""

    def get_store() -> Any:
        if store is not None:
            return store
        from deep_research.scale.storage import build_artifact_store

        return build_artifact_store()

    @app.post("/control/runs/{run_id}/artifacts", dependencies=auth, status_code=201)
    def save_artifact(run_id: str, body: ArtifactBody, format: str = Query(default="md", pattern="^(pdf|docx|pptx|json|md|html)$"),
                      x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        from deep_research.export import ExportUnavailable
        from deep_research.scale.storage import save_export

        report = body.report
        if report is None:
            try:
                result = manager.get(run_id).result
            except KeyError:
                raise HTTPException(status_code=404, detail="run not found") from None
            report = result.get("report") if isinstance(result, dict) else None
        if not isinstance(report, dict):
            raise HTTPException(status_code=409, detail="run has no report yet")
        try:
            info = save_export(get_store(), run_id, report, format)
        except ExportUnavailable as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from None
        except Exception as exc:  # storage unreachable: report the type only (no endpoint or credentials)
            raise HTTPException(status_code=503, detail=f"artifact store unavailable ({type(exc).__name__})") from None
        manager.audit.record({"event": "artifact_saved", "run_id": run_id, "key": info["key"], "store": info["store"],
                              "by": _operator(x_operator)})
        return info

    @app.get("/control/artifacts", dependencies=auth)
    def list_artifacts(run_id: str | None = Query(default=None, pattern="^[A-Za-z0-9_-]{1,64}$")) -> list[dict[str, Any]]:
        try:
            return list(get_store().list(f"runs/{run_id}/" if run_id else ""))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"artifact store unavailable ({type(exc).__name__})") from None

    @app.get("/control/artifacts/{key:path}", dependencies=auth)
    def get_artifact(key: str) -> Response:
        try:
            data = get_store().get(key)
        except (KeyError, ValueError):
            raise HTTPException(status_code=404, detail="artifact not found") from None
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"artifact store unavailable ({type(exc).__name__})") from None
        name = key.rsplit("/", 1)[-1]
        return Response(data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _share_routes(app: FastAPI, manager: RunManager, assistant: Any, auth: list[Any], ttl_days: float = 7.0) -> None:  # noqa: C901 - flat route table
    """Revocable read-only share links: create/list/revoke need the bearer token, viewing needs the share token."""
    from fastapi.responses import HTMLResponse

    from deep_research.assistant.share import SHARE_HEADERS, ShareService

    shares = ShareService(assistant, ttl_days)

    def report_for(run_id: str, body: ShareBody) -> dict[str, Any]:
        try:
            result = manager.get(run_id).result
        except KeyError:
            result = None
        report = result.get("report") if isinstance(result, dict) else None
        if not isinstance(report, dict):
            report = body.report
        if not isinstance(report, dict) or not report.get("content"):
            raise HTTPException(status_code=409, detail="run has no report to share")
        return report

    @app.post("/control/runs/{run_id}/shares", dependencies=auth, status_code=201)
    def create_share(run_id: str, body: ShareBody, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
            raise HTTPException(status_code=422, detail="invalid run id")
        info, token = shares.create(run_id, report_for(run_id, body), expires_in_days=body.expires_in_days)
        manager.audit.record({"event": "share_created", "share_id": info["id"], "run_id": run_id, "expires_at": info["expires_at"],
                              "by": _operator(x_operator)})
        return {**info, "path": f"/share/{info['id']}?token={token}"}

    @app.get("/control/shares", dependencies=auth)
    def list_shares(run_id: str | None = None) -> list[dict[str, Any]]:
        return list(shares.list(run_id))

    @app.post("/control/shares/{share_id}/revoke", dependencies=auth)
    def revoke_share(share_id: str, x_operator: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            info = shares.revoke(share_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="share not found") from None
        manager.audit.record({"event": "share_revoked", "share_id": share_id, "by": _operator(x_operator)})
        return dict(info)

    @app.get("/share/{share_id}", include_in_schema=False)
    def view_share(share_id: str, token: str = Query(default="", max_length=128)) -> Response:
        html = shares.open(share_id, token)
        if html is None:
            return Response("Share link not found or revoked.", status_code=404, media_type="text/plain",
                            headers=SHARE_HEADERS)
        return HTMLResponse(html, headers=SHARE_HEADERS)
