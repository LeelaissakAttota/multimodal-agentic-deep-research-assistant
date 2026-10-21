"""Secret redaction shared by structured logs and control-API run logs."""

from __future__ import annotations

import os
import re
import traceback
from collections.abc import Mapping, MutableMapping
from types import TracebackType
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
    "credential",
)
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{4,}"), r"\1 " + REDACTED),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|key)"
            r"(\s*[=:]\s*)(\"?)[^\s&\"',;]+"
        ),
        r"\1\2\3" + REDACTED,
    ),
    (re.compile(r"\bsk-or-[A-Za-z0-9_\-]{8,}"), REDACTED),  # OpenRouter
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bnvapi-[A-Za-z0-9_\-]{8,}"), REDACTED),  # NVIDIA
    (re.compile(r"\bhf_[A-Za-z0-9]{8,}"), REDACTED),  # Hugging Face
    (re.compile(r"\btvly-[A-Za-z0-9_\-]{8,}"), REDACTED),  # Tavily
)


def redact_text(text: str) -> str:
    """Mask API keys, Bearer tokens and key=value secrets inside free text."""

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(fragment in lowered for fragment in _SENSITIVE_KEY_FRAGMENTS)


def redact_value(key: str, value: Any) -> Any:
    """Redact a value by key name, recursing into containers and scanning strings."""

    if is_sensitive_key(key) and value not in (None, "", False):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, item) for item in value]
    return value


def redact_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: redact every field (including the event message)."""

    for key in list(event_dict.keys()):
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


def exception_where(tb: TracebackType | None) -> str | None:
    """``file.py:function:line`` of the innermost frame (no paths, no values)."""

    if tb is None:
        return None
    frames = traceback.extract_tb(tb)
    if not frames:
        return None
    last = frames[-1]
    return f"{os.path.basename(last.filename)}:{last.name}:{last.lineno}"
