"""Every dashboard page renders without an exception (offline, no API running)."""

from __future__ import annotations

from pathlib import Path

import pytest

PAGES = ["dashboard", "new", "researches", "knowledge", "sources", "tools", "learning", "settings"]


@pytest.mark.parametrize("page", PAGES)
def test_every_page_renders(page: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    testing = pytest.importorskip("streamlit.testing.v1")
    from tests.unit.test_dashboard_assistant import APP, _setup

    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("DEEP_RESEARCH_MEMORY_DIR", str(tmp_path / "mem"))
    at = testing.AppTest.from_file(APP, default_timeout=60)
    at.query_params.update({"page": page})
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.markdown, page
