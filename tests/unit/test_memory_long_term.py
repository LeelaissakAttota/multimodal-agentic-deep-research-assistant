from uuid import UUID

from deep_research.memory.research_memory import MemoryQuery, SemanticResearchMemory
from deep_research.memory.vector_store import LocalVectorStore
from deep_research.persistence.contracts import ResearchSessionSnapshot
from tests.unit.test_research_intelligence import make_snapshot


class _Repo:
    def __init__(self) -> None:
        self.s: dict[UUID, ResearchSessionSnapshot] = {}

    def initialize(self) -> None: ...

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        self.s[snapshot.state.id] = snapshot

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        return self.s[session_id]

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        return list(self.s.values())[:limit]

    def delete_session(self, session_id: UUID) -> bool:
        return self.s.pop(session_id, None) is not None


def test_semantic_query_ranks_relevant_evidence_with_provenance() -> None:
    mem = SemanticResearchMemory(_Repo())
    mem.store(make_snapshot())
    assert mem.vector_store.count() == 2
    recs = mem.query(MemoryQuery(objective="solar battery storage reliability", limit=5))
    assert recs[0].evidence.id == UUID(int=1006)
    assert recs[0].claims and recs[0].task_ids == [UUID(int=1003)]
    assert 0 < recs[0].relevance_score <= 1000


def test_cross_session_retrieval_and_session_filter() -> None:
    mem = SemanticResearchMemory(_Repo())
    mem.store(make_snapshot())
    mem.store(make_snapshot(session_id=UUID(int=2000), request_id=UUID(int=2001)))
    recs = mem.query(MemoryQuery(objective="quantum networking photons", limit=10))
    assert {r.session_id for r in recs if r.evidence.id == UUID(int=1007)} == {UUID(int=1000), UUID(int=2000)}
    only = mem.query(MemoryQuery(objective="quantum networking", session_id=UUID(int=2000)))
    assert only and {r.session_id for r in only} == {UUID(int=2000)}


def test_falls_back_to_lexical_when_empty_index_or_query() -> None:
    repo = _Repo()
    repo.save_session(make_snapshot())
    mem = SemanticResearchMemory(repo, store=LocalVectorStore())
    lex = mem.query(MemoryQuery(objective="solar"))
    assert lex and lex[0].relevance_score < 10
    assert mem.index_existing() == 2
    assert len(mem.query(MemoryQuery())) == 2


def test_stale_index_entries_are_skipped() -> None:
    repo = _Repo()
    mem = SemanticResearchMemory(repo)
    mem.store(make_snapshot())
    repo.delete_session(UUID(int=1000))
    repo.save_session(make_snapshot(session_id=UUID(int=3000)))
    assert all(r.session_id == UUID(int=3000) for r in mem.query(MemoryQuery(objective="solar storage")))
