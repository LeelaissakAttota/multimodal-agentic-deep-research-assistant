"""Local secrets file for project API keys (outside the repo, 0600).

Keys are never returned in full by any public method other than ``get`` (used
in-process to build provider settings). ``masked()`` exposes only the last 4
characters. The file is re-read when its mtime changes so updates apply to the
next run without a restart.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
import threading
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_MAX_VALUE = 4096


def default_config_dir(app: str) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (
        os.environ.get("APPDATA") if os.name == "nt" else None
    )
    root = Path(base) if base else Path.home() / ".config"
    return root / app


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return "****" + value[-4:]


class SecretStore:
    """JSON secrets file restricted to an allow-list of key names."""

    def __init__(self, path: Path, allowed: tuple[str, ...]) -> None:
        self._path = path
        self._allowed = frozenset(allowed)
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def allowed(self) -> tuple[str, ...]:
        return tuple(sorted(self._allowed))

    def _load(self) -> dict[str, str]:
        try:
            mtime = self._path.stat().st_mtime
        except FileNotFoundError:
            self._cache, self._mtime = {}, None
            return {}
        if self._mtime == mtime:
            return self._cache
        raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        data = {k: v for k, v in raw.items() if k in self._allowed and isinstance(v, str)}
        self._cache, self._mtime = data, mtime
        return data

    def get(self, name: str) -> str | None:
        with self._lock:
            value = self._load().get(name)
        return value or None

    def masked(self) -> dict[str, dict[str, object]]:
        with self._lock:
            data = self._load()
        return {
            name: {"configured": bool(data.get(name)), "masked": mask(data.get(name, ""))}
            for name in sorted(self._allowed)
        }

    def set(self, name: str, value: str) -> None:
        if name not in self._allowed or not _NAME_RE.match(name):
            raise KeyError(name)
        value = value.strip()
        if len(value) > _MAX_VALUE or any(ord(c) < 32 for c in value):
            raise ValueError("invalid secret value")
        with self._lock:
            data = dict(self._load())
            if value:
                data[name] = value
            else:
                data.pop(name, None)
            self._write(data)

    def _write(self, data: dict[str, str]) -> None:
        directory = self._path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, stat.S_IRWXU)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".secrets-")
        try:
            if sys.platform != "win32":  # sys.platform lets mypy narrow; os.fchmod is POSIX-only
                os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._mtime = None
