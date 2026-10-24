"""Project configuration checks (pytest import path, console scripts)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pytest_pythonpath_includes_src_and_root() -> None:
    assert CONFIG["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src", "."]
