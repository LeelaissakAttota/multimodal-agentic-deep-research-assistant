"""Normalize user-entered domain names (blocked-domain preferences)."""

from __future__ import annotations

import re

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://")


def normalize_domain(raw: str) -> str:
    """'https://www.Spam.net:8080/x?y' -> 'spam.net'; strips scheme, userinfo, path, port, trailing dots, 'www.'/'*.'.

    Returns '' for empty input. Validation of the result is the caller's job.
    """
    d = (raw or "").strip().lower()
    d = _SCHEME.sub("", d)
    if d.startswith("//"):
        d = d[2:]
    d = re.split(r"[/?#\\]", d, maxsplit=1)[0]
    d = d.rsplit("@", 1)[-1]
    if d.startswith("["):  # IPv6 literal: keep as-is (without port)
        return d.split("]", 1)[0] + "]" if "]" in d else d
    d = d.split(":", 1)[0].rstrip(".")
    for prefix in ("*.", "www."):
        d = d.removeprefix(prefix)
    return d
