"""
AEOS RAG Engine — Backward-Compatibility Facade
Wraps KnowledgePipeline to preserve the existing interface used by:
  - app/main.py  (/rag/ingest, /rag/query routes)
  - app/github_analyzer/indexer.py
  - Any external agent code that calls get_rag_engine()

New code should import KnowledgePipeline from app.rag.pipeline directly.

Namespace isolation:
    Each namespace maps to a separate VectorStore collection.
    Use different namespaces to isolate knowledge domains:
        get_rag_engine("github")     # GitHub repository index
        get_rag_engine("user_docs")  # user-uploaded documents
        get_rag_engine()             # default general knowledge base
"""
from __future__ import annotations

from app.rag.pipeline import KnowledgePipeline
from app.rag.retriever import RetrievedDocument
from app.rag.schemas import Document
from app.core.logger import get_logger

log = get_logger(__name__)

# ── Engine registry — one pipeline per namespace ───────────────────────────────
_pipeline_registry: dict[str, KnowledgePipeline] = {}


def get_rag_engine(namespace: str = "default") -> "RAGEngine":
    """
    Returns (or creates) a RAGEngine for the given namespace.
    Thread-safe for read access; construction is synchronous and cheap.
    """
    if namespace not in _pipeline_registry:
        _pipeline_registry[namespace] = KnowledgePipeline(namespace=namespace)
    # Wrap in the legacy facade so the call-site interface is unchanged
    return RAGEngine(_pipeline_registry[namespace])


class RAGEngine:
    """
    Backward-compatible facade over KnowledgePipeline.
    Exposes the same method signatures as the original RAGEngine
    so all existing routes and agent code continues to work unchanged.

    Do NOT add new features here — extend KnowledgePipeline instead.
    """

    def __init__(self, pipeline: KnowledgePipeline) -> None:
        self._pipeline = pipeline

    async def initialize(self) -> None:
        """Pre-warm the embedding model. Called during app lifespan startup."""
        await self._pipeline.initialize()

    # ── Query ──────────────────────────────────────────────────────────────────

    def query(
        self,
        text: str,
        filters: dict | None = None,
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        """
        Retrieve the most relevant chunks for a query string.
        Returns empty list when the knowledge base is empty.
        """
        return self._pipeline.search(query=text, top_k=top_k, filters=filters)

    # ── Ingestion ──────────────────────────────────────────────────────────────

    def ingest_file(self, path: str) -> int:
        """Ingest a single file. Returns the number of chunks added."""
        stats = self._pipeline.ingest_file(path)
        return stats.chunks_stored

    def ingest_directory(self, path: str, extensions: list[str] | None = None) -> int:
        """Ingest all files in a directory. Returns total chunks added."""
        stats = self._pipeline.ingest_directory(path, extensions=extensions)
        return stats.chunks_stored

    def ingest_text(self, text: str, source: str = "inline", doc_type: str = "text") -> int:
        """Ingest a raw text string. Returns number of chunks added."""
        stats = self._pipeline.ingest(text=text, source=source, doc_type=doc_type)
        return stats.chunks_stored

    def ingest_documents(self, documents: list[Document]) -> int:
        """Ingest pre-built Document objects. Returns total chunks added."""
        stats = self._pipeline.ingest_documents(documents)
        return stats.chunks_stored

    # ── Utility ────────────────────────────────────────────────────────────────

    def store_count(self) -> int:
        """Total chunk count currently stored in this namespace."""
        return self._pipeline.store_count()

    def reset(self) -> None:
        """Drop and recreate the store. Destructive — use in tests only."""
        self._pipeline.reset()
