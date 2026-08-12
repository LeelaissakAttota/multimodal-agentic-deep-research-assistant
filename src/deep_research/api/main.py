"""
FastAPI application for the deep research assistant.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from deep_research.core.config import settings
from deep_research.observability.logging import configure_logging, get_logger

# Configure logging
configure_logging()
logger = get_logger(__name__)

app = FastAPI(
    title=settings.app_name,
    version=settings.version,
    description="A multimodal agentic deep research assistant",
)


@app.get("/health", tags=["monitoring"])
async def health_check() -> JSONResponse:
    """
    Health check endpoint: returns 200 if the process is alive.
    """
    logger.debug("Health check requested")
    return JSONResponse(content={"status": "ok"}, status_code=200)


@app.get("/ready", tags=["monitoring"])
async def ready_check() -> JSONResponse:
    """
    Readiness check endpoint: returns 200 if the application foundation is ready.
    In Phase 1, we consider the application ready if the configuration is loaded.
    """
    logger.debug("Readiness check requested")
    # In a more complete implementation, we would check dependencies (e.g., database, external services).
    # For Phase 1, we assume that if the configuration is loaded, we are ready.
    return JSONResponse(content={"status": "ready"}, status_code=200)


@app.get("/version", tags=["monitoring"])
async def version_info() -> JSONResponse:
    """
    Version information endpoint.
    """
    logger.debug("Version info requested")
    return JSONResponse(
        content={
            "app_name": settings.app_name,
            "version": settings.version,
            "environment": settings.environment,
        },
        status_code=200,
    )


# Note: In later phases, we will include routers for research operations.
