"""Deterministic, network-free SQLite persistence adapter."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from pydantic import ValidationError

from deep_research.persistence.contracts import (
    CURRENT_SCHEMA_VERSION,
    PersistenceConflictError,
    PersistenceError,
    PersistenceNotFoundError,
    PersistenceSchemaError,
    ResearchSessionSnapshot,
    SecretDataError,
)
from deep_research.security import find_sensitive_data_path


class SQLiteResearchRepository:
    """SQLite implementation storing one atomic JSON aggregate per session.

    SQLite is part of Python's standard library, requires no service or network,
    and provides the transactional boundary needed for recoverable local state.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        if self.database_path.name in {"", ".", ".."}:
            raise ValueError("database_path must identify a SQLite file")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        component TEXT PRIMARY KEY,
                        version INTEGER NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS research_sessions (
                        session_id TEXT PRIMARY KEY,
                        request_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        schema_version INTEGER NOT NULL,
                        saved_at TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                row = connection.execute(
                    "SELECT version FROM schema_metadata WHERE component = ?",
                    ("research_sessions",),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO schema_metadata(component, version) VALUES (?, ?)",
                        ("research_sessions", CURRENT_SCHEMA_VERSION),
                    )
                elif int(row["version"]) != CURRENT_SCHEMA_VERSION:
                    raise PersistenceSchemaError(
                        "Unsupported SQLite research schema version: "
                        f"{row['version']}"
                    )
                connection.commit()
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Unable to initialize research persistence",
                error_code="persistence_initialization_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    def save_session(self, snapshot: ResearchSessionSnapshot) -> None:
        self.initialize()
        payload = self._serialize(snapshot)
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_row = connection.execute(
                    "SELECT payload FROM research_sessions WHERE session_id = ?",
                    (str(snapshot.state.id),),
                ).fetchone()
                if existing_row is not None:
                    existing = self._deserialize(str(existing_row["payload"]))
                    self._ensure_history_is_immutable(existing, snapshot)
                connection.execute(
                    """
                    INSERT INTO research_sessions(
                        session_id, request_id, status, schema_version, saved_at, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        request_id = excluded.request_id,
                        status = excluded.status,
                        schema_version = excluded.schema_version,
                        saved_at = excluded.saved_at,
                        payload = excluded.payload
                    """,
                    (
                        str(snapshot.state.id),
                        str(snapshot.request.id),
                        snapshot.state.status,
                        snapshot.schema_version,
                        snapshot.saved_at.isoformat(),
                        payload,
                    ),
                )
                connection.commit()
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Unable to save research session",
                error_code="persistence_write_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    def load_session(self, session_id: UUID) -> ResearchSessionSnapshot:
        self.initialize()
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT payload FROM research_sessions WHERE session_id = ?",
                    (str(session_id),),
                ).fetchone()
            if row is None:
                raise PersistenceNotFoundError(
                    f"Research session not found: {session_id}",
                    error_code="session_not_found",
                )
            return self._deserialize(str(row["payload"]))
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Unable to load research session",
                error_code="persistence_read_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    def list_sessions(self, limit: int = 100) -> list[ResearchSessionSnapshot]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.initialize()
        try:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT payload FROM research_sessions
                    ORDER BY saved_at DESC, session_id ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [self._deserialize(str(row["payload"])) for row in rows]
        except PersistenceError:
            raise
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Unable to list research sessions",
                error_code="persistence_read_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    def delete_session(self, session_id: UUID) -> bool:
        self.initialize()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "DELETE FROM research_sessions WHERE session_id = ?",
                    (str(session_id),),
                )
                connection.commit()
                return cursor.rowcount > 0
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Unable to delete research session",
                error_code="persistence_delete_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _deserialize(payload: str) -> ResearchSessionSnapshot:
        try:
            snapshot = ResearchSessionSnapshot.model_validate_json(payload)
        except (ValidationError, ValueError) as exc:
            raise PersistenceSchemaError(
                "Stored research session is invalid",
                error_code="invalid_persisted_session",
            ) from exc
        if snapshot.schema_version != CURRENT_SCHEMA_VERSION:
            raise PersistenceSchemaError(
                f"Unsupported snapshot schema version: {snapshot.schema_version}"
            )
        return snapshot

    @classmethod
    def _serialize(cls, snapshot: ResearchSessionSnapshot) -> str:
        try:
            payload_data = snapshot.model_dump(mode="json")
            sensitive_path = find_sensitive_data_path(payload_data, "snapshot")
            if sensitive_path is not None:
                raise SecretDataError(
                    f"Refusing to persist secret-like field at {sensitive_path}",
                    error_code="secret_data_rejected",
                )
            return json.dumps(
                payload_data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except PersistenceError:
            raise
        except (TypeError, ValueError) as exc:
            raise PersistenceError(
                "Research session contains data that cannot be serialized safely",
                error_code="persistence_serialization_failed",
                details={"exception_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _ensure_history_is_immutable(
        existing: ResearchSessionSnapshot,
        replacement: ResearchSessionSnapshot,
    ) -> None:
        for label, old_items, new_items in (
            ("source", existing.sources, replacement.sources),
            ("evidence", existing.evidence, replacement.evidence),
            ("claim", existing.claims, replacement.claims),
        ):
            new_by_id = {item.id: item for item in new_items}
            for old_item in old_items:
                new_item = new_by_id.get(old_item.id)
                if new_item is None or new_item != old_item:
                    raise PersistenceConflictError(
                        f"Refusing to remove or mutate historical {label} {old_item.id}",
                        error_code="immutable_history_conflict",
                    )
