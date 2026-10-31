"""Safe API error handlers shared by the research API and the control API.

* 422 responses list field + reason only; the submitted input value is never echoed.
* 4xx are logged with route, method and status; crashes log exception_where.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from deep_research.observability.logging import get_logger
from deep_research.observability.redaction import exception_where

logger = get_logger("deep_research.api.errors")


def _route(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


def safe_validation_details(exc: RequestValidationError) -> list[dict[str, Any]]:
    return [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "invalid")),
            "type": str(error.get("type", "validation_error")),
        }
        for error in exc.errors()
    ]


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = safe_validation_details(exc)
        logger.warning(
            "request_validation_failed",
            route=_route(request),
            method=request.method,
            status_code=422,
            fields=[".".join(d["loc"]) for d in details],
            reasons=[d["type"] for d in details],
        )
        return JSONResponse(status_code=422, content={"detail": details})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        log = logger.warning if exc.status_code < 500 else logger.error
        log(
            "http_error",
            route=_route(request),
            method=request.method,
            status_code=exc.status_code,
            detail=str(exc.detail),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_exception",
            route=_route(request),
            method=request.method,
            error_type=type(exc).__name__,
            exception_where=exception_where(exc.__traceback__),
        )
        return JSONResponse(status_code=500, content={"detail": "internal server error"})
