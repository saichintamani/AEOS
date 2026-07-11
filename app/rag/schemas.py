"""
AEOS RAG — Unified Data Schemas
Single source of truth for all data contracts flowing through the RAG pipeline.

Every module consumes these types. Never duplicate these dataclasses elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Core ingestion types ───────────────────────────────────────────────────────

@dataclass
class Document:
    """
    Canonical representation of an ingested document.
    Produced by any Loader. Consumed by Chunkers and the Pipeline.
    chunks are populated by the chunker after loading.
    """
    id: str                              # sha256[:16] of content
    source: str                          # file path, URL, or descriptive label
    content: str                         # raw extracted text
    doc_type: str                        # pdf | markdown | txt | html | json | python | ...
    language: str = "en"                 # ISO 639-1 language code
    metadata: dict = field(default_factory=dict)
    chunks: list["Chunk"] = field(default_factory=list)
    ingested_at: str = field(default_factory=_utc_now)


@dataclass
class Chunk:
    """
    A sub-document unit produced by a Chunker.
    Carries full provenance metadata required by the retrieval pipeline.
    """
    text: str
    index: int                           # position within parent document (0-based)
    token_count: int
    doc_id: str = ""                     # parent Document.id
    source: str = ""                     # inherited from parent document
    section: str = ""                    # heading or section name (when detectable)
    page: int = -1                       # page number; -1 = unknown
    language: str = "en"                 # ISO 639-1
    timestamp: str = field(default_factory=_utc_now)
    metadata: dict = field(default_factory=dict)


# ── Embedding types ────────────────────────────────────────────────────────────

@dataclass
class EmbeddingResult:
    """Result of a batch embedding call from any EmbeddingService."""
    texts: list[str]
    vectors: list[list[float]]
    model: str
    dimension: int
    latency_ms: float


# ── Retrieval types ────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """Raw output from a VectorStore similarity search (pre-ranking)."""
    id: str
    text: str
    score: float               # cosine similarity 0.0–1.0 (higher = more similar)
    metadata: dict
    rank: int = 0


@dataclass
class RetrievalResult:
    """
    Post-ranking, post-assembly result delivered to agents.
    This is the final per-chunk result type in a ContextPackage.
    """
    text: str
    score: float               # combined dense + BM25 score, 0.0–1.0
    rank: int
    source: str                # file path or label of the originating document
    doc_id: str                # parent document id
    chunk_index: int           # position within parent document
    metadata: dict


@dataclass
class ContextPackage:
    """
    Final assembled context package returned to the agent or orchestrator.
    Contains ranked results plus full pipeline performance metrics.
    """
    query: str
    results: list[RetrievalResult]
    total_retrieved: int
    total_tokens: int               # approximate token count across all result texts
    retrieval_latency_ms: float     # end-to-end retrieval time
    embedding_latency_ms: float     # time spent embedding the query
    search_latency_ms: float        # time spent in vector store similarity search
    metadata: dict = field(default_factory=dict)

    def as_context_string(self, separator: str = "\n\n---\n\n") -> str:
        """
        Concatenate all result texts into a single string for LLM prompt injection.
        Ordered by rank (best first).
        """
        return separator.join(r.text for r in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "results": [
                {
                    "text": r.text,
                    "score": r.score,
                    "rank": r.rank,
                    "source": r.source,
                    "doc_id": r.doc_id,
                    "chunk_index": r.chunk_index,
                    "metadata": r.metadata,
                }
                for r in self.results
            ],
            "metrics": {
                "total_retrieved": self.total_retrieved,
                "total_tokens": self.total_tokens,
                "retrieval_latency_ms": self.retrieval_latency_ms,
                "embedding_latency_ms": self.embedding_latency_ms,
                "search_latency_ms": self.search_latency_ms,
            },
        }


# ── Pipeline operation stats ───────────────────────────────────────────────────

@dataclass
class IngestStats:
    """Returned by KnowledgePipeline.ingest*() to summarise what happened."""
    documents_processed: int
    chunks_created: int
    chunks_stored: int
    failed_documents: int
    latency_ms: float
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents_processed": self.documents_processed,
            "chunks_created": self.chunks_created,
            "chunks_stored": self.chunks_stored,
            "failed_documents": self.failed_documents,
            "latency_ms": self.latency_ms,
            "sources": self.sources,
        }
