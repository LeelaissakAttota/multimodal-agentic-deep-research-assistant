"""
Observability module: logging configuration.
"""
import logging
import sys

import structlog
from typing import cast

from deep_research.core.config import settings


def configure_logging() -> None:
    """
    Configure structured logging for the application.
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
            structlog.processors.JSONRenderer(),
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
