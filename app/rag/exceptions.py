"""
AEOS RAG — Custom Exception Hierarchy
All RAG subsystem errors inherit from RAGError.

Callers may catch the base class for broad handling or specific
subtypes for fine-grained error routing and logging.

Hierarchy:
    RAGError
    ├── LoaderError        — document loading / parsing failures
    ├── ChunkingError      — chunker cannot process content
    ├── EmbeddingError     — embedding model / API failure
    ├── VectorStoreError   — backend read / write / connection failure
    ├── RetrievalError     — retrieval pipeline stage failure
    └── PipelineError      — top-level KnowledgePipeline failure
"""
from __future__ import annotations
from typing import Any


class RAGError(Exception):
    """
    Base exception for all AEOS RAG subsystem errors.
    Carries a human-readable message and an optional details dict
    for structured logging.
    """

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | {self.details}"
        return self.message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.message!r}, details={self.details!r})"


class LoaderError(RAGError):
    """
    Raised when a document loader fails to read or parse a source.
    Examples: file not found, unsupported encoding, malformed PDF, oversized file.
    """


class ChunkingError(RAGError):
    """
    Raised when a chunker cannot process document content.
    Examples: unknown strategy name, tokeniser import failure.
    """


class EmbeddingError(RAGError):
    """
    Raised when an embedding service fails.
    Examples: model load failure, API key missing, provider rate-limit.
    """


class VectorStoreError(RAGError):
    """
    Raised when a vector store backend operation fails.
    Examples: ChromaDB connection refused, add/query/delete failure.
    """


class RetrievalError(RAGError):
    """
    Raised when any stage in the RetrievalPipeline fails.
    Examples: query embedding failure, empty store, BM25 scoring error.
    """


class PipelineError(RAGError):
    """
    Raised by KnowledgePipeline when an end-to-end operation fails.
    Wraps lower-level errors (Loader, Embedding, VectorStore, Retrieval)
    and adds pipeline-level context.
    """
