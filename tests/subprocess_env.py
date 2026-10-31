"""Minimal-but-working environments for test subprocesses (Linux, macOS and Windows)."""

from __future__ import annotations

import os
from collections.abc import Mapping

#: OS variables a child Python needs. On Windows, a missing SYSTEMROOT breaks socket/asyncio imports
#: (``import _overlapped`` -> WinError 10106) and TEMP/USERPROFILE are needed by tempfile and site.
KEEP_OS_VARS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
                "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
                "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "LC_CTYPE")


def minimal_env(extra: Mapping[str, str] | None = None, *, source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Only OS essentials from ``source`` (default ``os.environ``; no app keys or secrets) plus ``extra``."""
    src = os.environ if source is None else source
    wanted = {k.upper() for k in KEEP_OS_VARS}
    env = {k: v for k, v in src.items() if k.upper() in wanted}
    env.update(extra or {})
    return env
