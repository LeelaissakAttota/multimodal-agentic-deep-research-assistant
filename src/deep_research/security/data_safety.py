"""Deterministic detection of secret-like fields and credential-bearing URLs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

_SECRET_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "client_secret",
        "cookie",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "set_cookie",
    }
)
_SECRET_SUFFIXES = ("_api_key", "_password", "_secret", "_token")


def find_sensitive_data_path(value: Any, path: str = "data") -> str | None:
    """Return the first secret-like location without returning its value."""
    if isinstance(value, Mapping):
        return _find_mapping_path(value, path)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _find_sequence_path(value, path)
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return path if _url_contains_credentials(value) else None
    return None


def _find_mapping_path(value: Mapping[Any, Any], path: str) -> str | None:
    for raw_key, nested_value in value.items():
        key = str(raw_key).strip().lower().replace("-", "_")
        nested_path = f"{path}.{raw_key}"
        if key in _SECRET_KEYS or key.endswith(_SECRET_SUFFIXES):
            return nested_path
        found = find_sensitive_data_path(nested_value, nested_path)
        if found is not None:
            return found
    return None


def _find_sequence_path(value: Sequence[Any], path: str) -> str | None:
    for index, nested_value in enumerate(value):
        found = find_sensitive_data_path(nested_value, f"{path}[{index}]")
        if found is not None:
            return found
    return None


def _url_contains_credentials(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.username is not None or parsed.password is not None
