"""Optional SQLAlchemy/Postgres session repository + migration CLI (SQLite stays the default)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("sqlalchemy")

from deep_research.memory.factory import build_long_term_memory
from deep_research.persistence.contracts import (
    PersistenceConflictError,
    PersistenceNotFoundError,
    PersistenceSchemaError,
    ResearchSessionRepository,
    SecretDataError,
)
from deep_research.persistence.factory import build_session_repository, open_repository
from deep_research.persistence.sqlalchemy_repository import SQLAlchemyResearchRepository, normalize_database_url, safe_url
from deep_research.persistence.sqlite_repository import SQLiteResearchRepository
from deep_research.scale import backend_status
from deep_research.scale.migrate import main as migrate
from tests.unit.test_research_intelligence import make_snapshot


@pytest.fixture()
def repo(tmp_path: Path) -> SQLAlchemyResearchRepository:
    return SQLAlchemyResearchRepository(f"sqlite:///{(tmp_path / 'sa.db').as_posix()}")


def test_sqlite_is_default(tmp_path: Path) -> None:
    r = build_session_repository(tmp_path / "m.sqlite")
    assert isinstance(r, SQLiteResearchRepository) and r.database_path == tmp_path / "m.sqlite"
    assert backend_status()[1]["state"] == "off"
    mem = build_long_term_memory("lexical", tmp_path)
    assert mem is not None and (tmp_path / "research_memory.sqlite").exists()


def test_env_selects_sqlalchemy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_DATABASE_URL", f"sqlite:///{(tmp_path / 'pg-standin.db').as_posix()}")
    r = build_session_repository(tmp_path / "m.sqlite")
    assert isinstance(r, SQLAlchemyResearchRepository) and isinstance(r, ResearchSessionRepository)
    mem = build_long_term_memory("lexical", tmp_path)
    assert mem is not None and not (tmp_path / "research_memory.sqlite").exists()


def test_contract_roundtrip(repo: SQLAlchemyResearchRepository) -> None:
    repo.initialize()
    repo.initialize()
    snap = make_snapshot()
    repo.save_session(snap)
    repo.save_session(snap)  # idempotent upsert
    assert repo.load_session(snap.state.id) == snap
    other = make_snapshot(session_id=UUID(int=2000), request_id=UUID(int=2001))
    repo.save_session(other)
    assert len(repo.list_sessions()) == 2 and len(repo.list_sessions(limit=1)) == 1
    with pytest.raises(ValueError):
        repo.list_sessions(limit=0)
    assert repo.delete_session(other.state.id) and not repo.delete_session(other.state.id)
    with pytest.raises(PersistenceNotFoundError):
        repo.load_session(other.state.id)


def test_same_rules_as_sqlite(repo: SQLAlchemyResearchRepository) -> None:
    snap = make_snapshot()
    repo.save_session(snap)
    changed = snap.model_copy(deep=True)
    changed.evidence[0].content = "rewritten"
    with pytest.raises(PersistenceConflictError, match="historical evidence"):
        repo.save_session(changed)
    leaky = make_snapshot(session_id=UUID(int=3000))
    leaky.request.metadata["api_key"] = "must-not-be-written"
    with pytest.raises(SecretDataError):
        repo.save_session(leaky)


def test_schema_version_mismatch(repo: SQLAlchemyResearchRepository) -> None:
    repo.initialize()
    with repo.engine.begin() as conn:
        conn.execute(repo.schema_table.update().values(version=999))
    fresh = SQLAlchemyResearchRepository(repo.url, engine=repo.engine)
    with pytest.raises(PersistenceSchemaError):
        fresh.initialize()


def test_url_helpers_hide_credentials() -> None:
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalize_database_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
    shown = safe_url("postgresql://mdra:s3cretpw@db.local:5432/mdra")
    assert "s3cretpw" not in shown and "mdra:" not in shown and "db.local" in shown


def test_status_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MDRA_DATABASE_URL", "postgresql://mdra:s3cretpw@db.local:5432/mdra")
    row = backend_status()[1]
    assert row["configured"] and "s3cretpw" not in json.dumps(row)


def test_migrate_copy_export_import(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "old.sqlite"
    sqlite_repo = SQLiteResearchRepository(src)
    snap = make_snapshot()
    sqlite_repo.save_session(snap)
    dst_url = f"sqlite:///{(tmp_path / 'new.db').as_posix()}"
    monkeypatch.setenv("MDRA_DATABASE_URL", "postgresql://mdra:s3cretpw@db.local:5432/mdra")
    assert migrate(["copy", "--from", str(src), "--to", dst_url]) == 0
    assert open_repository(dst_url).load_session(snap.state.id) == snap
    out = tmp_path / "exp" / "sessions.jsonl"
    assert migrate(["export", "--from", dst_url, "--out", str(out)]) == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 1
    third = tmp_path / "third.sqlite"
    assert migrate(["import", "--to", str(third), "--in", str(out)]) == 0
    assert SQLiteResearchRepository(third).load_session(snap.state.id) == snap
    printed = capsys.readouterr().out
    assert "copied 1" in printed and "exported 1" in printed and "imported 1" in printed and "s3cretpw" not in printed


@pytest.mark.skipif(not os.environ.get("MDRA_TEST_DATABASE_URL"), reason="set MDRA_TEST_DATABASE_URL to test real Postgres")
def test_real_postgres_roundtrip() -> None:
    repo = SQLAlchemyResearchRepository(os.environ["MDRA_TEST_DATABASE_URL"])
    snap = make_snapshot(session_id=UUID(int=9_000_001))
    repo.delete_session(snap.state.id)
    repo.save_session(snap)
    assert repo.load_session(snap.state.id) == snap
    assert repo.delete_session(snap.state.id)


def _row_counts(db: Path) -> dict[str, int]:
    import sqlite3

    with sqlite3.connect(db) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {t: int(con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]) for t in tables}


def test_migrate_twice_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "old.sqlite"
    sqlite_repo = SQLiteResearchRepository(src)
    snap = make_snapshot()
    sqlite_repo.save_session(snap)
    dst_db = tmp_path / "new.db"
    dst_url = f"sqlite:///{dst_db.as_posix()}"
    out = tmp_path / "sessions.jsonl"
    third = tmp_path / "third.sqlite"

    assert migrate(["copy", "--from", str(src), "--to", dst_url]) == 0
    assert migrate(["export", "--from", dst_url, "--out", str(out)]) == 0
    assert migrate(["import", "--to", str(third), "--in", str(out)]) == 0
    first = (_row_counts(dst_db), _row_counts(third))
    assert sum(first[0].values()) > 0 and sum(first[1].values()) > 0

    assert migrate(["copy", "--from", str(src), "--to", dst_url]) == 0
    assert migrate(["import", "--to", str(third), "--in", str(out)]) == 0
    assert (_row_counts(dst_db), _row_counts(third)) == first  # no duplicate rows anywhere
    for target in (dst_url, str(third)):
        repo = open_repository(target)
        assert len(repo.list_sessions(limit=100)) == 1 and repo.load_session(snap.state.id) == snap
    capsys.readouterr()
