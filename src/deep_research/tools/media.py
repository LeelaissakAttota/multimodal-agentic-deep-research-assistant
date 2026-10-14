"""Shared helpers for binary media payloads (base64 data URLs)."""

from __future__ import annotations

import base64
import binascii


class MediaDecodeError(ValueError):
    """Raised when a media payload is not a valid base64 data URL."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def decode_data_url(data: str, *, allowed_mimes: frozenset[str], max_bytes: int) -> tuple[bytes, str]:
    """Decode ``data:<mime>;base64,<payload>`` into (bytes, mime).

    Enforces the MIME allow-list and the decoded size limit (checked on the
    encoded length first so oversized input is never fully decoded).
    """
    if not isinstance(data, str) or not data.strip():
        raise MediaDecodeError("Payload must be a non-empty data URL string.", "missing_input")
    text = data.strip()
    if not text.lower().startswith("data:") or ";base64," not in text[:200].lower():
        raise MediaDecodeError("Payload must be a 'data:<mime>;base64,<data>' URL.", "unsupported_representation")
    header, payload = text.split(",", 1)
    mime = header[5:].split(";", 1)[0].strip().lower()
    if mime not in allowed_mimes:
        raise MediaDecodeError(f"Unsupported media type '{mime}'.", "unsupported_type")
    if len(payload) > (max_bytes * 4) // 3 + 4:
        raise MediaDecodeError(f"Payload exceeds the {max_bytes // (1024 * 1024)} MiB limit.", "too_large")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise MediaDecodeError("Payload is not valid base64.", "invalid_base64")
    if len(raw) > max_bytes:
        raise MediaDecodeError(f"Payload exceeds the {max_bytes // (1024 * 1024)} MiB limit.", "too_large")
    if not raw:
        raise MediaDecodeError("Payload is empty.", "missing_input")
    return raw, mime


def to_data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"
