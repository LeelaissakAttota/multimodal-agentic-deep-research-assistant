"""Pick the session repository: SQLite file by default, SQLAlchemy/Postgres when ``MDRA_DATABASE_URL`` is set."""

from __future__ import annotations

from pathlib import Path

from deep_research.persistence.contracts import ResearchSessionRepository


def open_repository(target: str | Path) -> ResearchSessionRepository:
    """``target`` is a SQLAlchemy URL (``scheme://...``) or a SQLite file path."""
    text = str(target)
    if "://" in text:
        if text.startswith("sqlite:///"):
            from deep_research.persistence.sqlite_repository import SQLiteResearchRepository

            return SQLiteResearchRepository(text[len("sqlite:///"):])
        from deep_research.persistence.sqlalchemy_repository import SQLAlchemyResearchRepository

        return SQLAlchemyResearchRepository(text)
    from deep_research.persistence.sqlite_repository import SQLiteResearchRepository

    return SQLiteResearchRepository(Path(text))


def build_session_repository(default_sqlite_path: str | Path, database_url: str | None = None) -> ResearchSessionRepository:
    """``MDRA_DATABASE_URL`` (or ``database_url``) wins; otherwise the SQLite file, exactly as before."""
    if database_url is None:
        from deep_research.scale.config import ScaleSettings

        database_url = ScaleSettings().secret("database_url")
    if database_url:
        from deep_research.persistence.sqlalchemy_repository import SQLAlchemyResearchRepository

        return SQLAlchemyResearchRepository(database_url)
    from deep_research.persistence.sqlite_repository import SQLiteResearchRepository

    return SQLiteResearchRepository(Path(default_sqlite_path))
