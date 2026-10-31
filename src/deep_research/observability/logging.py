"""
Observability module: logging configuration.
"""
import logging
import sys

import structlog
from typing import Any, cast

from deep_research.core.config import settings
from deep_research.observability.redaction import redact_event


def _renderer(log_format: str | None) -> Any:
    chosen = (log_format or settings.log_format or "json").strip().lower()
    if chosen == "text":
        # One readable line per event: timestamp level event key=value ...
        return structlog.processors.KeyValueRenderer(
            key_order=["timestamp", "level", "event"], drop_missing=True
        )
    return structlog.processors.JSONRenderer()


def configure_logging(log_format: str | None = None) -> None:
    """
    Configure structured logging for the application.

    ``DEEP_RESEARCH_LOG_FORMAT=text`` gives readable one-line logs; JSON is the default.
    Every event passes through secret redaction before rendering.
    """
    # Configure standard logging to output JSON via structlog
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=settings.log_level.upper(),
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            redact_event,
            _renderer(log_format),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelName(settings.log_level.upper())
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a structured logger for the given name.
    """
    return cast(structlog.BoundLogger, structlog.get_logger(name))
