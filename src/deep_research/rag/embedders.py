"""Embedding providers for RAG: local sentence-transformers (default) or a free OpenAI-compatible API.

:func:`build_embedder` returns ``None`` when the selected provider cannot run (missing
``sentence-transformers``, no API key, a non-free model); callers then use word-overlap retrieval.
The API provider only accepts OpenRouter ``:free`` models or a server on localhost (Ollama,
LM Studio, llama.cpp), so it can never spend money.
"""

from __future__ import annotations

import importlib.util
import math
from typing import Any
from urllib.parse import urlparse

from deep_research.memory.embeddings import Embedder
from deep_research.rag.config import RagSettings

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


class EmbeddingUnavailable(RuntimeError):
    """The configured embeddings provider cannot be used (reason is safe to show)."""


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def free_api_model(api_base: str, model: str) -> bool:
    """OpenRouter-style ``:free`` model, or any model served from this machine."""
    host = (urlparse(api_base).hostname or "").lower()
    return model.endswith(":free") or host in _LOCAL_HOSTS


class LocalSTEmbedder:
    """``sentence-transformers`` model run locally (``memory`` extra), L2-normalised vectors."""

    def __init__(self, model_name: str, model: Any | None = None) -> None:
        if model is None:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(model_name)
        self._model = model
        self.name = model_name
        self.dimension = int(model.get_sentence_embedding_dimension())

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [[float(v) for v in row] for row in vectors]


class OpenAICompatEmbedder:
    """POST ``{api_base}/embeddings`` (OpenAI wire format; OpenRouter, Ollama, LM Studio...)."""

    def __init__(self, api_base: str, model: str, api_key: str = "", *, timeout: float = 30.0,
                 client: Any | None = None, batch_size: int = 64) -> None:
        if not free_api_model(api_base, model):
            raise EmbeddingUnavailable(f"embeddings model {model!r} is not free (use a ':free' model or a local server)")
        self.api_base = api_base.rstrip("/")
        self.name = model
        self.dimension = 0  # learned from the first response
        self._key = api_key
        self._timeout = timeout
        self._client = client
        self._batch = batch_size

    def _post(self, texts: list[str]) -> list[list[float]]:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            resp = client.post(f"{self.api_base}/embeddings", json={"model": self.name, "input": texts}, headers=headers)
        finally:
            if self._client is None:
                client.close()
        if resp.status_code != 200:
            raise EmbeddingUnavailable(f"embeddings endpoint returned HTTP {resp.status_code}")
        try:
            rows = sorted(resp.json()["data"], key=lambda d: int(d.get("index", 0)))
            vectors = [_normalise([float(v) for v in row["embedding"]]) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            raise EmbeddingUnavailable("embeddings endpoint returned an unexpected body") from exc
        if len(vectors) != len(texts) or len({len(v) for v in vectors}) > 1:
            raise EmbeddingUnavailable("embeddings endpoint returned mismatched vectors")
        return vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            out.extend(self._post(texts[i:i + self._batch]))
        if out:
            self.dimension = len(out[0])
        return out


def _api_key(settings: RagSettings) -> str:
    if settings.api_key is not None and settings.api_key.get_secret_value().strip():
        return settings.api_key.get_secret_value().strip()
    if (urlparse(settings.api_base).hostname or "").endswith("openrouter.ai"):
        from deep_research.core.config import Settings

        key = Settings().openrouter_api_key
        return key.get_secret_value().strip() if key is not None else ""
    return ""


def embeddings_ready(settings: RagSettings) -> tuple[bool, str]:
    """Cheap check (no model load, no network): can the configured provider run here?"""
    if settings.embeddings == "local":
        if importlib.util.find_spec("sentence_transformers") is None:
            return False, "sentence-transformers not installed (pip install -e '.[rag]')"
        return True, f"{settings.model} (local)"
    if not free_api_model(settings.api_base, settings.api_model):
        return False, f"{settings.api_model} is not a free model"
    host = (urlparse(settings.api_base).hostname or "").lower()
    if host not in _LOCAL_HOSTS and not _api_key(settings):
        return False, "no API key for the embeddings endpoint"
    return True, f"{settings.api_model} (API)"


def build_embedder(settings: RagSettings) -> Embedder | None:
    """The configured free embedder, or ``None`` when it cannot run (word-overlap fallback)."""
    ok, _ = embeddings_ready(settings)
    if not ok:
        return None
    try:
        if settings.embeddings == "local":
            return LocalSTEmbedder(settings.model)
        return OpenAICompatEmbedder(settings.api_base, settings.api_model, _api_key(settings),
                                    timeout=settings.timeout_seconds)
    except Exception:  # noqa: BLE001 - weights not downloadable offline, bad model name, ...
        return None
