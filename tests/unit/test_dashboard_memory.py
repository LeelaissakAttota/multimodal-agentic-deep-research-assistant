from pathlib import Path
from typing import Any

from deep_research.dashboard.core import memory_items, memory_search, memory_status


def _run(rid: str, title: str, claim: str, cite: str) -> dict[str, Any]:
    return {"run_id": rid, "created_at": "2026-10-24T10:00:00Z", "params": {"objective": title},
            "result": {"report": {"title": title, "claims": [{"text": claim, "confidence": 0.8}],
                                  "citations": [{"number": 1, "title": cite, "url": f"https://x.org/{rid}"}]}}}


RUNS = [_run("r1", "Solar storage", "Battery storage reliability improved in field tests.", "Solar field study"),
        _run("r2", "Medieval poetry", "Chaucer wrote in Middle English.", "Poetry anthology"), {"run_id": "r3"}]


def test_memory_items_from_real_reports() -> None:
    items = memory_items(RUNS)
    assert {i["kind"] for i in items} == {"claim", "source"} and len(items) == 4


def test_memory_search_ranks_relevant_first() -> None:
    hits = memory_search(RUNS, "battery storage reliability")
    assert hits[0]["run_id"] == "r1" and hits[0]["kind"] == "claim" and hits[0]["score"] > 0
    assert memory_search(RUNS, "  ") == [] and memory_search([], "x") == []


def test_memory_status_reports_backends(tmp_path: Path) -> None:
    (tmp_path / "research_memory.json").write_text("{}", encoding="utf-8")
    ms = memory_status(tmp_path)
    assert ms["bytes_on_disk"] == 2 and ms["path"] == str(tmp_path)
    assert "fallback" in ms["embedder"] or "local" in ms["embedder"]
    assert memory_status(tmp_path / "missing")["bytes_on_disk"] == 0
