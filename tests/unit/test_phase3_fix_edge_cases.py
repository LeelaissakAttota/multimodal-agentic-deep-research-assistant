"""Edge cases for Phase 3 fixes: memory factory/env, vector store JSON, cosine mismatch, KB search."""

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.config import Settings
from deep_research.memory import factory
from deep_research.memory.embeddings import cosine
from deep_research.memory.research_memory import BoundedResearchMemory, SemanticResearchMemory
from deep_research.memory.vector_store import LocalVectorStore

REPO_DATA = Path(__file__).resolve().parents[2] / "data" / factory.MEMORY_DB_NAME


@pytest.fixture(autouse=True)
def _no_heavy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("chromadb", "sentence_transformers"):
        monkeypatch.setitem(sys.modules, name, None)


@pytest.mark.parametrize("value", ["auto", "semantic", "lexical", "off", "OFF"])
def test_env_values_accepted(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_RESEARCH_LONG_TERM_MEMORY", value)
    assert Settings().long_term_memory == value.lower()  # modes are case-insensitive


def test_env_invalid_value_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEP_RESEARCH_LONG_TERM_MEMORY", "vector")
    with pytest.raises(ValidationError):
        Settings()


def test_conftest_default_is_off() -> None:
    assert Settings().long_term_memory == "off"


@pytest.mark.parametrize("mode,kind", [("lexical", BoundedResearchMemory), ("semantic", SemanticResearchMemory)])
def test_factory_modes(mode: str, kind: type, tmp_path: Path) -> None:
    mem = factory.build_long_term_memory(mode, tmp_path / "nested" / "dir")
    assert mem is not None and isinstance(mem.memory, kind) and mem.mode == mode
    assert (tmp_path / "nested" / "dir" / factory.MEMORY_DB_NAME).exists()


def test_factory_off_writes_nothing(tmp_path: Path) -> None:
    assert factory.build_long_term_memory("off", tmp_path / "x") is None
    assert not (tmp_path / "x").exists()


def test_auto_falls_back_to_lexical_without_deps(tmp_path: Path) -> None:
    assert not factory.semantic_extra_available()
    mem = factory.build_long_term_memory("auto", tmp_path)
    assert mem is not None and mem.mode == "lexical"


def test_factory_invalid_mode_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        factory.build_long_term_memory("bogus", tmp_path)
    assert not list(tmp_path.iterdir())


def test_memory_mode_case_insensitive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    assert factory.resolve_memory_mode("  OFF ") == "off"
    assert factory.build_long_term_memory(" Off", tmp_path) is None
    mem = factory.build_long_term_memory("LEXICAL", tmp_path)
    assert mem is not None and mem.mode == "lexical"
    monkeypatch.setenv("DEEP_RESEARCH_LONG_TERM_MEMORY", " Semantic ")
    assert Settings().long_term_memory == "semantic"
    monkeypatch.setenv("DEEP_RESEARCH_LONG_TERM_MEMORY", "bogus")
    with pytest.raises(ValidationError):
        Settings()


def test_app_auto_writes_only_under_memory_dir(tmp_path: Path) -> None:
    mdir = tmp_path / "mem"
    existed = REPO_DATA.exists()
    app = ResearchApplication(Settings(long_term_memory="auto", memory_dir=str(mdir)))
    asyncio.run(app.submit(ResearchSubmission(objective="grid battery storage")))
    assert (mdir / factory.MEMORY_DB_NAME).exists()
    assert REPO_DATA.exists() == existed


@pytest.mark.parametrize("payload", ["[]", "5", '"s"', "null", "{bad"])
def test_non_dict_json_warns_and_is_usable(payload: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "c.json").write_text(payload, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        store = LocalVectorStore(tmp_path, "c")
    assert store.count() == 0 and caplog.records
    store.upsert(["a"], [[1.0, 0.0]], ["t"], [{}])
    assert json.loads((tmp_path / "c.json").read_text())["a"]["text"] == "t"


def test_cosine_mismatch_raises_and_search_skips(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError):
        cosine([1.0], [1.0, 2.0])
    assert cosine([], []) == 0.0
    store = LocalVectorStore()
    store.upsert(["a", "b"], [[1.0, 0.0], [1.0, 0.0, 0.0]], ["x", "y"], [{}, {}])
    with caplog.at_level(logging.WARNING):
        hits = store.search([1.0, 0.0], 5)
    assert [h.id for h in hits] == ["a"] and "skipped 1" in caplog.text
    assert store.search([1.0, 0.0], 0) == [] and store.search([1.0, 0.0], -1) == []


def _runs() -> list[dict[str, object]]:
    return [{"run_id": "r1", "result": {"session_id": "s1", "report": {"title": "Solar",
             "content": "Solar battery storage improves grid reliability."}}}]


def test_kb_search_without_persistent_store(tmp_path: Path) -> None:
    from deep_research.dashboard.core import memory_search

    for vdir in (None, tmp_path / "missing", tmp_path):
        res = memory_search(_runs(), "solar battery", vector_dir=vdir)  # type: ignore[arg-type]
        assert all(r["backend"] == "in-memory" for r in res)


def test_kb_search_uses_persistent_store(tmp_path: Path) -> None:
    from deep_research.dashboard.core import memory_search
    from deep_research.memory.embeddings import get_embedder

    vec = get_embedder().embed(["solar battery storage"])[0]
    LocalVectorStore(tmp_path).upsert(["m1"], [vec], ["solar battery storage"], [{"session_id": "s1"}])
    res = memory_search(_runs(), "solar battery", vector_dir=tmp_path)  # type: ignore[arg-type]
    assert res and res[0]["backend"] == "persistent" and res[0]["run_id"] == "r1"
    # blank query never hits persistent path
    assert all(r.get("backend") != "persistent" for r in memory_search(_runs(), "   ", vector_dir=tmp_path))  # type: ignore[arg-type]


def test_kb_search_corrupt_store_falls_back(tmp_path: Path) -> None:
    from deep_research.dashboard.core import memory_search

    (tmp_path / "research_memory.json").write_text("[1,2]", encoding="utf-8")
    res = memory_search(_runs(), "solar", vector_dir=tmp_path)  # type: ignore[arg-type]
    assert all(r["backend"] == "in-memory" for r in res)
