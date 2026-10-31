"""Vector index for RAG chunks: pgvector on Postgres, or a local FAISS/numpy store.

Both backends keep chunks of different embedding models apart (``model`` key), replace a run's
chunks atomically on re-index, and answer exact cosine-similarity queries (vectors are unit length,
so inner product == cosine). ``auto`` picks pgvector only when ``MDRA_DATABASE_URL`` is set and the
``vector`` extension exists (or can be created); otherwise the local store under ``MDRA_RAG_INDEX_DIR``.

pgvector schema (one table, created on first use)::

    mdra_rag_chunks(model text, chunk_id text, run_id text, kind text, ord int, text text,
                    citations text /* JSON list */, title text, section text, doc_hash text,
                    embedding vector, created_at timestamptz, PRIMARY KEY (model, chunk_id))
    index mdra_rag_chunks_run (model, run_id)
    mdra_rag_docs(model text, run_id text, doc_hash text, chunks int, updated_at timestamptz,
                  PRIMARY KEY (model, run_id))   -- also records reports that produced 0 chunks
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Protocol

from deep_research.rag.chunking import Chunk
from deep_research.rag.config import RagSettings

TABLE = "mdra_rag_chunks"
DOCS = "mdra_rag_docs"


class ChunkIndex(Protocol):
    name: str

    def doc_hash(self, run_id: str) -> str | None: ...

    def replace_run(self, run_id: str, chunks: list[Chunk], vectors: list[list[float]], doc_hash: str = "") -> None: ...

    def chunks(self, run_id: str | None = None) -> list[Chunk]: ...

    def vector_search(self, vector: list[float], k: int, run_id: str | None = None) -> list[tuple[Chunk, float]]: ...

    def count(self) -> int: ...


def model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", model).strip("._") or "model"


class LocalIndex:
    """JSON-lines file per embedding model; searched with FAISS when installed, else numpy, else pure Python."""

    def __init__(self, directory: Path, model: str) -> None:
        self.path = Path(directory) / model_slug(model) / "chunks.jsonl"
        self.model = model
        has_faiss = importlib.util.find_spec("faiss") is not None
        self._np = importlib.util.find_spec("numpy") is not None
        self.name = "faiss (local)" if has_faiss and self._np else "numpy (local)" if self._np else "python (local)"
        self.docs_path = self.path.with_name("docs.json")
        self._rows: list[tuple[Chunk, list[float]]] | None = None
        self._docs: dict[str, str] | None = None

    def _load_docs(self) -> dict[str, str]:
        """run_id -> doc_hash of every indexed report, including ones with 0 chunks."""
        if self._docs is None:
            docs: dict[str, str] = {}
            try:
                raw = json.loads(self.docs_path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    docs = {str(k): v for k, v in raw.items() if isinstance(v, str)}
            except (OSError, ValueError):
                pass
            self._docs = docs
        return self._docs

    def _load(self) -> list[tuple[Chunk, list[float]]]:
        if self._rows is None:
            rows: list[tuple[Chunk, list[float]]] = []
            if self.path.is_file():
                for line in self.path.read_text(encoding="utf-8").splitlines():
                    try:
                        data = json.loads(line)
                        rows.append((Chunk.from_dict(data["chunk"]), [float(v) for v in data["vector"]]))
                    except (ValueError, KeyError, TypeError):
                        continue  # skip a torn line instead of failing the whole index
            self._rows = rows
        return self._rows

    def doc_hash(self, run_id: str) -> str | None:
        docs = self._load_docs()
        if run_id in docs:
            return docs[run_id]
        return next((c.doc_hash for c, _ in self._load() if c.run_id == run_id), None)  # index from before docs.json

    def replace_run(self, run_id: str, chunks: list[Chunk], vectors: list[list[float]], doc_hash: str = "") -> None:
        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        rows = [r for r in self._load() if r[0].run_id != run_id] + list(zip(chunks, vectors))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        tmp.write_text("".join(json.dumps({"chunk": c.as_dict(), "vector": v}) + "\n" for c, v in rows), encoding="utf-8")
        os.replace(tmp, self.path)
        self._rows = rows
        docs = dict(self._load_docs())
        doc_hash = doc_hash or (chunks[0].doc_hash if chunks else "")
        if doc_hash:
            docs[run_id] = doc_hash
        else:
            docs.pop(run_id, None)
        tmp = self.docs_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(docs, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.docs_path)
        self._docs = docs

    def chunks(self, run_id: str | None = None) -> list[Chunk]:
        return [c for c, _ in self._load() if run_id is None or c.run_id == run_id]

    def count(self) -> int:
        return len(self._load())

    def vector_search(self, vector: list[float], k: int, run_id: str | None = None) -> list[tuple[Chunk, float]]:
        rows = [r for r in self._load() if (run_id is None or r[0].run_id == run_id) and len(r[1]) == len(vector)]
        if not rows or k <= 0:
            return []
        if self._np:
            scores = self._scores_np(vector, [v for _, v in rows], k)
        else:
            scores = sorted(((sum(a * b for a, b in zip(v, vector)), i) for i, (_, v) in enumerate(rows)), reverse=True)[:k]
        return [(rows[i][0], float(s)) for s, i in scores]

    def _scores_np(self, vector: list[float], matrix: list[list[float]], k: int) -> list[tuple[float, int]]:
        import numpy as np

        mat = np.asarray(matrix, dtype="float32")
        query = np.asarray([vector], dtype="float32")
        k = min(k, len(matrix))
        if self.name.startswith("faiss"):
            import faiss

            flat = faiss.IndexFlatIP(mat.shape[1])
            flat.add(mat)
            dist, idx = flat.search(query, k)
            return [(float(d), int(i)) for d, i in zip(dist[0], idx[0]) if i >= 0]
        sims = mat @ query[0]
        order = np.argsort(-sims)[:k]
        return [(float(sims[i]), int(i)) for i in order]


def _vec_literal(vector: list[float]) -> str:
    if not all(math.isfinite(v) for v in vector):
        raise ValueError("vector contains non-finite values")
    return "[" + ",".join(f"{v:.7g}" for v in vector) + "]"


class PgVectorIndex:
    """Chunks in Postgres with the pgvector extension (exact cosine search with ``<=>``)."""

    name = "pgvector"

    def __init__(self, engine: Any, model: str) -> None:
        self.engine = engine
        self.model = model
        self._ready = False

    @staticmethod
    def available(engine: Any) -> bool:
        """True when the ``vector`` extension exists or could be created by this user."""
        try:
            import sqlalchemy as sa
        except ImportError:  # postgres extra not installed
            return False
        try:
            with engine.begin() as conn:
                try:
                    with conn.begin_nested():
                        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
                except Exception:  # noqa: BLE001 - not installed on the server or no privilege
                    pass
                return conn.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first() is not None
        except Exception:  # noqa: BLE001 - unreachable server, wrong credentials, not Postgres
            return False

    def _ensure(self) -> None:
        if self._ready:
            return
        import sqlalchemy as sa

        with self.engine.begin() as conn:
            conn.execute(sa.text(
                f"CREATE TABLE IF NOT EXISTS {TABLE} (model text NOT NULL, chunk_id text NOT NULL, run_id text NOT NULL, "
                "kind text NOT NULL, ord integer NOT NULL, text text NOT NULL, citations text NOT NULL DEFAULT '[]', "
                "title text NOT NULL DEFAULT '', section text NOT NULL DEFAULT '', doc_hash text NOT NULL DEFAULT '', "
                "embedding vector NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (model, chunk_id))"))
            conn.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {TABLE}_run ON {TABLE} (model, run_id)"))
            conn.execute(sa.text(
                f"CREATE TABLE IF NOT EXISTS {DOCS} (model text NOT NULL, run_id text NOT NULL, doc_hash text NOT NULL, "
                "chunks integer NOT NULL DEFAULT 0, updated_at timestamptz NOT NULL DEFAULT now(), "
                "PRIMARY KEY (model, run_id))"))
        self._ready = True

    @staticmethod
    def _chunk(row: Any) -> Chunk:
        m = row._mapping
        return Chunk(m["chunk_id"], m["run_id"], m["kind"], int(m["ord"]), m["text"], [int(n) for n in json.loads(m["citations"])],
                     m["title"], m["section"], m["doc_hash"])

    _COLS = "chunk_id, run_id, kind, ord, text, citations, title, section, doc_hash"

    def doc_hash(self, run_id: str) -> str | None:
        import sqlalchemy as sa

        self._ensure()
        with self.engine.connect() as conn:
            row = conn.execute(sa.text(f"SELECT doc_hash FROM {DOCS} WHERE model = :m AND run_id = :r"),
                               {"m": self.model, "r": run_id}).first()
            if row is None:  # chunks indexed before mdra_rag_docs existed
                row = conn.execute(sa.text(f"SELECT doc_hash FROM {TABLE} WHERE model = :m AND run_id = :r LIMIT 1"),
                                   {"m": self.model, "r": run_id}).first()
        return str(row[0]) if row else None

    def replace_run(self, run_id: str, chunks: list[Chunk], vectors: list[list[float]], doc_hash: str = "") -> None:
        import sqlalchemy as sa

        if len(chunks) != len(vectors):
            raise ValueError("chunks and vectors differ in length")
        self._ensure()
        rows = [{"m": self.model, "id": c.chunk_id, "r": run_id, "k": c.kind, "o": c.ord, "t": c.text,
                 "c": json.dumps(c.citations), "ti": c.title, "s": c.section, "h": c.doc_hash, "e": _vec_literal(v)}
                for c, v in zip(chunks, vectors)]
        with self.engine.begin() as conn:  # one transaction: readers never see a half-indexed run
            conn.execute(sa.text(f"DELETE FROM {TABLE} WHERE model = :m AND run_id = :r"), {"m": self.model, "r": run_id})
            if rows:
                conn.execute(sa.text(
                    f"INSERT INTO {TABLE} (model, chunk_id, run_id, kind, ord, text, citations, title, section, doc_hash, "
                    "embedding) VALUES (:m, :id, :r, :k, :o, :t, :c, :ti, :s, :h, CAST(:e AS vector))"), rows)
            doc_hash = doc_hash or (chunks[0].doc_hash if chunks else "")
            if doc_hash:
                conn.execute(sa.text(
                    f"INSERT INTO {DOCS} (model, run_id, doc_hash, chunks) VALUES (:m, :r, :h, :n) ON CONFLICT (model, run_id) "
                    "DO UPDATE SET doc_hash = EXCLUDED.doc_hash, chunks = EXCLUDED.chunks, updated_at = now()"),
                    {"m": self.model, "r": run_id, "h": doc_hash, "n": len(chunks)})

    def chunks(self, run_id: str | None = None) -> list[Chunk]:
        import sqlalchemy as sa

        self._ensure()
        where = "model = :m" + (" AND run_id = :r" if run_id is not None else "")
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(f"SELECT {self._COLS} FROM {TABLE} WHERE {where} ORDER BY run_id, ord"),
                                {"m": self.model, "r": run_id}).all()
        return [self._chunk(r) for r in rows]

    def count(self) -> int:
        import sqlalchemy as sa

        self._ensure()
        with self.engine.connect() as conn:
            return int(conn.execute(sa.text(f"SELECT count(*) FROM {TABLE} WHERE model = :m"), {"m": self.model}).scalar() or 0)

    def vector_search(self, vector: list[float], k: int, run_id: str | None = None) -> list[tuple[Chunk, float]]:
        import sqlalchemy as sa

        self._ensure()
        where = "model = :m AND vector_dims(embedding) = :d" + (" AND run_id = :r" if run_id is not None else "")
        sql = (f"SELECT {self._COLS}, 1 - (embedding <=> CAST(:e AS vector)) AS score FROM {TABLE} WHERE {where} "
               "ORDER BY embedding <=> CAST(:e AS vector) LIMIT :k")
        with self.engine.connect() as conn:
            rows = conn.execute(sa.text(sql), {"m": self.model, "d": len(vector), "r": run_id, "e": _vec_literal(vector),
                                               "k": int(k)}).all()
        return [(self._chunk(r), float(r._mapping["score"])) for r in rows]


def open_index(settings: RagSettings, model: str, *, database_url: str | None = None,
               engine: Any | None = None) -> tuple[ChunkIndex, str]:
    """``(index, reason)``: pgvector when allowed and available, else the local store."""
    local = LocalIndex(Path(settings.index_dir), model)
    if settings.index == "local":
        return local, "MDRA_RAG_INDEX=local"
    if engine is None:
        if database_url is None:
            from deep_research.scale.config import ScaleSettings

            database_url = ScaleSettings().secret("database_url")
        if not database_url:
            return local, "MDRA_DATABASE_URL not set"
        if importlib.util.find_spec("sqlalchemy") is None or importlib.util.find_spec("psycopg") is None:
            return local, "postgres extra not installed"
        import sqlalchemy as sa

        from deep_research.persistence.sqlalchemy_repository import normalize_database_url

        try:
            engine = sa.create_engine(normalize_database_url(database_url), pool_pre_ping=True, future=True)
        except Exception:  # noqa: BLE001 - malformed URL; never echo it (may hold a password)
            return local, "MDRA_DATABASE_URL is invalid"
    if not PgVectorIndex.available(engine):
        return local, "pgvector extension not available"
    return PgVectorIndex(engine, model), "pgvector"
