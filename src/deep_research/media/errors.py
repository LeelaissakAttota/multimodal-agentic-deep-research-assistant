"""Clear, user-facing errors for missing optional pieces."""

from __future__ import annotations

INSTALL_HINT = 'pip install -e ".[media]"'


class MediaError(RuntimeError):
    """Media could not be analysed; ``kind`` feeds ``failure_kind``."""

    def __init__(self, message: str, kind: str = "media_error") -> None:
        super().__init__(message)
        self.kind = kind


class MediaUnavailable(MediaError):
    """An optional dependency or binary is missing."""

    def __init__(self, what: str, hint: str = INSTALL_HINT) -> None:
        super().__init__(f"Media analysis needs {what}. Install it with: {hint}", "media_unavailable")
