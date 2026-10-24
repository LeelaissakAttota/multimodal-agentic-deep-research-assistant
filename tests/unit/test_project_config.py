"""Project configuration checks (pytest import path, console scripts)."""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_pytest_pythonpath_includes_src_and_root() -> None:
    assert CONFIG["tool"]["pytest"]["ini_options"]["pythonpath"] == ["src", "."]


def test_deep_research_eval_console_script_resolves() -> None:
    import importlib

    target = CONFIG["project"]["scripts"]["deep-research-eval"]
    module, func = target.split(":")
    assert target == "deep_research.evaluation.__main__:main"
    assert callable(getattr(importlib.import_module(module), func))
    assert getattr(importlib.import_module(module), func)(["--json"]) == 0
