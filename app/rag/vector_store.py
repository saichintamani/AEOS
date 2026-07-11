"""
AEOS RAG — Vector Store
Abstract interface + production-ready implementations.

Architecture:
    VectorStoreInterface (ABC)           — stable contract, never couple to backends
    ├── NumpyVectorStore                 — in-memory cosine sim, zero deps (dev/test)
    ├── ChromaVectorStore                — ChromaDB HttpClient, persistent (production)
    └── [Future slots]                   — FAISS | Pinecone | Milvus | Qdrant
                                           implement VectorStoreInterface, plug in

    VectorStore (facade)                 — auto-selects backend from config
                                           exposes add_chunks / query / delete / count

Backend selection (config-driven):
    CHROMA_HOST set  → ChromaVectorStore (production, persistent)
    CHROMA_HOST ""   → NumpyVectorStore  (development, in-memory)

Custom backend injection (testing / alternate provider):
    store = VectorStore(backend=MyFAISSBackend())
    pipeline = KnowledgePipeline(vector_store=store)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from app.rag.schemas import Chunk
from app.rag.exceptions import VectorStoreError
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


# ── Abstract interface ─────────────────────────────────────────────────────────

class VectorStoreInterface(ABC):
    """
    Every vector store backend must implement this interface.
    The VectorStore facade delegates entirely to an injected backend.
    Callers depend on VectorStore, never on concrete backends.
    """

    @abstractmethod
    def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Persist a batch of vectors with associated texts and metadata."""
        ...

    @abstractmethod
    def query(
        self,
        query_embedding: list[float],
        n_results: int,
    ) -> dict[str, list]:
        """
        Find the n_results nearest neighbours to query_embedding.
        Returns a dict with keys: ids, documents, metadatas, distances.
        Each value is a list-of-lists (ChromaDB batched-query convention).
        distances are in [0, 2] range (0 = identical, 2 = opposite under cosine).
        """
        ...

    @abstractmethod
    def delete(self, where: dict) -> None:
        """Delete all entries whose metadata matches the given filter."""
        ...

    @abstractmethod
    def count(self) -> int:
        """Return total number of stored vectors."""
        ...

    @abstractmethod
    def reset(self) -> None:
        """Clear all stored data. Destructive — use in tests only."""
        ...


# ── NumpyVectorStore ───────────────────────────────────────────────────────────

class NumpyVectorStore(VectorStoreInterface):
    """
    In-memory vector store backed by numpy cosine similarity.

    Properties:
    - Zero external dependencies
    - Starts instantly (no service needed)
    - O(n) query — fast for <100k chunks, acceptable up to ~500k
    - Not persistent: data lost on process restart

    Use for: development, unit tests, single-process deployments.
    """

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._vectors: list[list[float]] = []
        self._documents: list[str] = []
        self._metadatas: list[dict] = []

    def add(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        self._ids.extend(ids)
        self._vectors.extend(embeddings)
        self._documents.extend(documents)
        self._metadatas.extend(metadatas)

    def query(self, query_embedding: list[float], n_results: int) -> dict[str, list]:
        if not self._vectors:
            return {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}

        q = np.array(query_embedding, dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-10)

        mat = np.array(self._vectors, dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True) + 1e-10
        mat_normed = mat / norms

        # Cosine distance: 0 = identical, 2 = opposite
        distances = 1.0 - (mat_normed @ q_norm)

        n = min(n_results, len(self._ids))
        top_idx = np.argsort(distances)[:n].tolist()

        return {
            "ids":       [[self._ids[i]       for i in top_idx]],
            "documents": [[self._documents[i]  for i in top_idx]],
            "metadatas": [[self._metadatas[i]  for i in top_idx]],
            "distances": [[float(distances[i]) for i in top_idx]],
        }

    def delete(self, where: dict) -> None:
        doc_id = where.get("doc_id")
        if not doc_id:
            return
        keep = [i for i, m in enumerate(self._metadatas) if m.get("doc_id") != doc_id]
        self._ids       = [self._ids[i]       for i in keep]
        self._vectors   = [self._vectors[i]   for i in keep]
        self._documents = [self._documents[i] for i in keep]
        self._metadatas = [self._metadatas[i] for i in keep]

    def count(self) -> int:
        return len(self._ids)

    def reset(self) -> None:
        self._ids.clear()
        self._vectors.clear()
        self._documents.clear()
        self._metadatas.clear()


# ── ChromaVectorStore ──────────────────────────────────────────────────────────

class ChromaVectorStore(VectorStoreInterface):
    """
    Persistent vector store backed by ChromaDB.
    Requires: pip install chromadb
    Requires: a running ChromaDB server (set CHROMA_HOST / CHROMA_PORT).

    Properties:
    - HNSW cosine index (fast ANN, sub-linear query complexity)
    - Persistent across process restarts
    - Supports multi-process concurrent access
    - Server-side metadata filtering via WHERE clauses

    Use for: production, large corpora, multi-worker deployments.
    """

    def __init__(self, host: str, port: int, namespace: str) -> None:
        try:
            import chromadb
        except ImportError:
            raise VectorStoreError(
                "chromadb not installed. Run: pip install chromadb"
            )
        try:
            log.info(
                "ChromaDB connecting",
                extra={"ctx_host": host, "ctx_port": port, "ctx_ns": namespace},
            )
            self._client = chromadb.HttpClient(host=host, port=port)
            self._collection = self._client.get_or_create_collection(
                name=namespace,
                metadata={"hnsw:space": "cosine"},
            )
            log.info("ChromaDB connected", extra={"ctx_ns": namespace})
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                f"ChromaDB connection failed: {exc}",
                {"host": host, "port": port},
            ) from exc

    def add(self, ids, embeddings, documents, metadatas) -> None:
        try:
            self._collection.add(
                ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
            )
        except Exception as exc:
            raise VectorStoreError(f"ChromaDB add failed: {exc}") from exc

    def query(self, query_embedding, n_results) -> dict[str, list]:
        try:
            return self._collection.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as exc:
            raise VectorStoreError(f"ChromaDB query failed: {exc}") from exc

    def delete(self, where) -> None:
        try:
            self._collection.delete(where=where)
        except Exception as exc:
            raise VectorStoreError(f"ChromaDB delete failed: {exc}") from exc

    def count(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return 0

    def reset(self) -> None:
        log.warning("ChromaVectorStore.reset() is a no-op. Manage via ChromaDB admin API.")


# ── VectorStore facade ─────────────────────────────────────────────────────────

class VectorStore:
    """
    Public interface for all RAG components.

    Auto-selects backend from configuration:
        CHROMA_HOST configured → ChromaVectorStore (production)
        CHROMA_HOST empty      → NumpyVectorStore  (development/testing)

    Custom backend injection (testing, alternate providers):
        VectorStore(backend=MyCustomBackend())

    Exposes:
        add_chunks()      — store document chunks with embeddings
        query()           — similarity search, returns normalised score dicts
        delete_document() — remove all chunks for a given doc_id
        count()           — total chunk count
        reset()           — wipe store (test use only)
        backend_type      — human-readable backend class name
    """

    def __init__(
        self,
        namespace: str | None = None,
        backend: VectorStoreInterface | None = None,
    ) -> None:
        self._namespace = namespace or settings.chroma_collection

        if backend is not None:
            self._backend: VectorStoreInterface = backend
        elif settings.chroma_host:
            self._backend = ChromaVectorStore(
                host=settings.chroma_host,
                port=settings.chroma_port,
                namespace=self._namespace,
            )
        else:
            log.info(
                "VectorStore using NumpyVectorStore (in-memory)",
                extra={"ctx_namespace": self._namespace},
            )
            self._backend = NumpyVectorStore()

    # ── Write ──────────────────────────────────────────────────────────────────

    def add_chunks(
        self,
        chunks: list[Chunk],
        doc_id: str,
        embeddings: list[list[float]],
        extra_metadata: dict | None = None,
    ) -> None:
        """
        Store chunks with pre-computed embeddings.
        extra_metadata is merged into every chunk's stored metadata.
        """
        if not chunks:
            return
        extra = extra_metadata or {}
        ids       = [f"{doc_id}__chunk_{c.index}" for c in chunks]
        documents = [c.text for c in chunks]
        metadatas = [
            {
                **c.metadata,
                **extra,
                "doc_id": doc_id,
                "chunk_index": c.index,
                "source": c.source or extra.get("source", ""),
                "section": c.section,
                "page": c.page,
                "language": c.language,
                "timestamp": c.timestamp,
            }
            for c in chunks
        ]
        try:
            self._backend.add(
                ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
            )
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(
                f"Failed to store chunks: {exc}",
                {"doc_id": doc_id, "chunk_count": len(chunks)},
            ) from exc
        log.debug(
            "Chunks stored",
            extra={"ctx_doc_id": doc_id, "ctx_count": len(chunks)},
        )

    # ── Read ───────────────────────────────────────────────────────────────────

    def query(
        self,
        query_embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
    ) -> list[dict[str, Any]]:
        """
        Similarity search.
        Returns list of dicts with keys: id, text, score (0-1), metadata.
        score is normalised from cosine distance: score = 1 - (dist / 2).
        """
        n = min(top_k, max(1, self.count()))
        try:
            raw = self._backend.query(query_embedding=query_embedding, n_results=n)
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(f"Vector query failed: {exc}") from exc

        ids       = raw.get("ids",       [[]])[0]
        docs      = raw.get("documents", [[]])[0]
        metas     = raw.get("metadatas", [[]])[0]
        distances = raw.get("distances", [[]])[0]

        return [
            {
                "id":       rid,
                "text":     text,
                "score":    max(0.0, 1.0 - (dist / 2.0)),
                "metadata": meta or {},
            }
            for rid, text, meta, dist in zip(ids, docs, metas, distances)
        ]

    # ── Utility ────────────────────────────────────────────────────────────────

    def delete_document(self, doc_id: str) -> None:
        """Remove all chunks belonging to doc_id."""
        self._backend.delete(where={"doc_id": doc_id})
        log.debug("Document deleted from store", extra={"ctx_doc_id": doc_id})

    def count(self) -> int:
        try:
            return self._backend.count()
        except Exception:
            return 0

    def reset(self) -> None:
        self._backend.reset()
        log.debug("VectorStore reset", extra={"ctx_namespace": self._namespace})

    @property
    def backend_type(self) -> str:
        return type(self._backend).__name__
