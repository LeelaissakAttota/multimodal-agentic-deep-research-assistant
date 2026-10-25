"""Optional retrieval-augmented generation (RAG) for report follow-up chat and the Knowledge Base.

Off by default (``MDRA_RAG_ENABLED=false``). When enabled, report text, claims and cited source
excerpts are chunked (:mod:`.chunking`), embedded with a free model (:mod:`.embedders`), stored in a
vector index (:mod:`.index`: pgvector or a local FAISS/numpy store) and retrieved with hybrid
BM25 + vector search fused by reciprocal rank (:mod:`.hybrid`). Missing embeddings dependencies
fall back to the original word-overlap retrieval in :mod:`deep_research.assistant.followup`.
"""

from deep_research.rag.config import RagSettings

__all__ = ["RagSettings"]
