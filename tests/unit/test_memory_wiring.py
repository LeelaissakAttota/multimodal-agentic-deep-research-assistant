import asyncio
import sys
from pathlib import Path

import pytest

from deep_research.api.research_service import ResearchApplication, ResearchSubmission
from deep_research.core.config import Settings
from deep_research.memory import factory
from deep_research.memory.research_memory import BoundedResearchMemory, MemoryQuery, SemanticResearchMemory


def _settings(mode: str, tmp_path: Path) -> Settings:
    return Settings(long_term_memory=mode, memory_dir=str(tmp_path))  # type: ignore[arg-type]


def test_auto_mode_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(factory, "semantic_extra_available", lambda: False)
    assert factory.resolve_memory_mode("auto") == "lexical"
    monkeypatch.setattr(factory, "semantic_extra_available", lambda: True)
    assert factory.resolve_memory_mode("auto") == "semantic"
    assert factory.resolve_memory_mode("off") == "off"


def test_off_keeps_original_orchestrator(tmp_path: Path) -> None:
    app = ResearchApplication(_settings("off", tmp_path))
    orch = app._build_orchestrator()
    assert orch.session_repository is None and orch.context_builder is None
    assert not list(tmp_path.iterdir())


def test_lexical_fallback_persists_runs(tmp_path: Path) -> None:
    app = ResearchApplication(_settings("lexical", tmp_path))
    asyncio.run(app.submit(ResearchSubmission(objective="solar battery storage reliability")))
    mem = app.long_term_memory
    assert mem is not None and type(mem.memory) is BoundedResearchMemory
    assert (tmp_path / factory.MEMORY_DB_NAME).exists()
    sessions = mem.repository.list_sessions()
    assert sessions and sessions[0].evidence
    assert mem.memory.query(MemoryQuery(objective="solar battery storage"))


def test_semantic_mode_indexes_runs_without_heavy_deps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("chromadb", "sentence_transformers"):
        monkeypatch.setitem(sys.modules, name, None)
    app = ResearchApplication(_settings("semantic", tmp_path))
    asyncio.run(app.submit(ResearchSubmission(objective="solar battery storage reliability")))
    asyncio.run(app.submit(ResearchSubmission(objective="solar panel degradation")))
    mem = app.long_term_memory
    assert mem is not None and isinstance(mem.memory, SemanticResearchMemory)
    assert mem.memory.vector_store.count() > 0
    assert (tmp_path / "vector_store" / "research_memory.json").exists()
    assert mem.memory.query(MemoryQuery(objective="solar battery", limit=3))
    assert app.long_term_memory is mem


def test_materialize_is_idempotent_and_handles_bad_urls(tmp_path: Path) -> None:
    from tests.unit.test_research_intelligence import make_snapshot

    snap = make_snapshot()
    snap = snap.model_copy(update={"report_metadata": {
        "title": "T", "content": "Para one.\n\nPara two.",
        "claims": [{"text": "Claim A"}, "junk"], "citations": [{"title": "Bad", "url": "not a url"}, {"title": "Ok",
                                                                 "url": "https://ok.org"}, 5]}})
    once = factory.materialize_report_evidence(snap)
    twice = factory.materialize_report_evidence(once)
    assert len(once.evidence) == len(twice.evidence) == 2 + 1 + 2 + 2
    assert factory.materialize_report_evidence(make_snapshot().model_copy(update={"report_metadata": {}})).evidence == \
        make_snapshot().evidence
