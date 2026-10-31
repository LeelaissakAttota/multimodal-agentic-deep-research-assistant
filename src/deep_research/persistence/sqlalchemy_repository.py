"""Optional SQLAlchemy adapter (PostgreSQL via psycopg) for :class:`ResearchSessionRepository`.

Same aggregate model, validation, secret rejection and immutable-history rule as the SQLite
adapter; selected only when ``MDRA_DATABASE_URL`` is set. Needs the ``postgres`` extra.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from deep_research.persistence.contracts import (
    CURRENT_SCHEMA_VERSION,
    PersistenceError,
    PersistenceNotFoundError,
    PersistenceSchemaError,
    ResearchSessionSnapshot,
)
from deep_research.persistence.sqlite_repository import SQLiteResearchRepository as _Rules


def normalize_database_url(url: str) -> str:
    """``postgres://`` / ``postgresql://`` -> ``postgresql+psycopg://`` (psycopg 3, Windows wheels available)."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def safe_url(url: str) -> str:
    """URL for messages: scheme and host only, never user or password."""
    from sqlalchemy.engine import make_url

    try:
        parsed = make_url(url)
    except Exception:
        return "<invalid database url>"
    return f"{parsed.drivername}://{parsed.host or ''}{'/' + parsed.database if parsed.database else ''}"


class SQLAlchemyResearchRepository:
    """One JSON aggregate per session in ``research_sessions`` (portable SQL, no server-side JSON needed)."""

    def __init__(self, url: str, *, engine: Any | None = None) -> None:
        import sqlalchemy as sa

        self.url = normalize_database_url(url)
        self.engine = engine if engine is not None else sa.create_engine(self.url, pool_pre_ping=True, future=True)
        self.metadata = sa.MetaData()
        self.schema_table = sa.Table(
            "schema_metadata", self.metadata,
            sa.Column("component", sa.String(64), primary_key=True),
            sa.Column("version", sa.Integer, nullable=False),
        )
        self.sessions = sa.Table(
            "research_sessions", self.metadata,
            sa.Column("session_id", sa.String(36), primary_key=True),
            sa.Column("request_id", sa.String(36), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("schema_version", sa.Integer, nullable=False),
            sa.Column("saved_at", sa.String(40), nullable=False, index=True),
            sa.Column("payload", sa.Text, nullable=False),
        )
        self._initialized = False

    def _fail(self, message: str, code: str, exc: Exception) -> PersistenceError:
        return PersistenceError(message, error_code=code, details={"exception_type": type(exc).__name__})

    def initialize(self) -> None:
        if self._initialized:
            return
        import sqlalchemy as sa

        try:
            self.metadata.create_all(self.engine)
            with self.engine.begin() as conn:
                row = conn.execute(sa.select(self.schema_table.c.version)
                                   .where(self.schema_table.c.component == "research_sessions")).first()
                if row is None:
                    conn.execute(self.schema_table.insert().values(component="research_sessions", version=CURRENT_SCHEMA_VERSION))
                elif int(row[0]) != CURRENT_SCHEMA_VERSION:
                    raise PersistenceSchemaError(f"Unsupported research schema version: {row[0]}")
        except PersistenceError:
            raise
        except sa.exc.SQLAlchemyError as exc:
            raise self._fail("Unable to initialize research persistence", "persistence_initialization_failed", exc) from exc
        self._initialized = True

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        import sqlalchemy as sa

        self.initialize()
        payload = _Rules._serialize(snapshot)
        values = {"request_id": str(snapshot.request.id), "status": str(snapshot.state.status),
                  "schema_version": snapshot.schema_version, "saved_at": snapshot.saved_at.isoformat(), "payload": payload}
        key = str(snapshot.state.id)
        try:
            with self.engine.begin() as conn:
                query = sa.select(self.sessions.c.payload).where(self.sessions.c.session_id == key)
                if conn.dialect.name != "sqlite":
                    query = query.with_for_update()
                row = conn.execute(query).first()
                if row is None:
                    conn.execute(self.sessions.insert().values(session_id=key, **values))
                else:
                    _Rules._ensure_history_is_immutable(_Rules._deserialize(str(row[0])), snapshot)
                    conn.execute(self.sessions.update().where(self.sessions.c.session_id == key).values(**values))
        except PersistenceError:
            raise
        except sa.exc.SQLAlchemyError as exc:
            raise self._fail("Unable to save research session", "persistence_write_failed", exc) from exc

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        import sqlalchemy as sa

        self.initialize()
        try:
            with self.engine.connect() as conn:
                row = conn.execute(sa.select(self.sessions.c.payload).where(self.sessions.c.session_id == str(session_id))).first()
        except sa.exc.SQLAlchemyError as exc:
            raise self._fail("Unable to load research session", "persistence_read_failed", exc) from exc
        if row is None:
            raise PersistenceNotFoundError(f"Research session not found: {session_id}", error_code="session_not_found")
        return _Rules._deserialize(str(row[0]))

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        import sqlalchemy as sa

        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.initialize()
        try:
            with self.engine.connect() as conn:
                rows = conn.execute(sa.select(self.sessions.c.payload)
                                    .order_by(self.sessions.c.saved_at.desc(), self.sessions.c.session_id.asc())
                                    .limit(limit)).all()
        except sa.exc.SQLAlchemyError as exc:
            raise self._fail("Unable to list research sessions", "persistence_read_failed", exc) from exc
        return [_Rules._deserialize(str(r[0])) for r in rows]

    def delete_session(self, session_id: UUID) -> bool:
        import sqlalchemy as sa

        self.initialize()
        try:
            with self.engine.begin() as conn:
                result = conn.execute(self.sessions.delete().where(self.sessions.c.session_id == str(session_id)))
                return bool(result.rowcount)
        except sa.exc.SQLAlchemyError as exc:
            raise self._fail("Unable to delete research session", "persistence_delete_failed", exc) from exc
