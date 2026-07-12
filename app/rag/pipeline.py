"""
AEOS RAG — KnowledgePipeline
The single knowledge access layer for all AEOS agents and services.

Design rule: every agent must retrieve information ONLY through this class.
             Never import VectorStore, Retriever, or Embeddings directly in agents.
             The Orchestrator calls retrieve_context() before invoking an agent.

Responsibilities:
    1. Document ingestion   — load → chunk → embed → store
    2. Query processing     — embed → search → filter → rank
    3. Context assembly     — build ContextPackage for LLM injection
    4. Response preparation — provide typed results agents can consume

All components are injected (dependency injection) for testability:
    pipeline = KnowledgePipeline(
        embedding_service=OpenAIEmbeddings(),
        vector_store=VectorStore(backend=FAISSBackend()),
    )

Observability:
    Every operation emits structured log events with:
        ctx_docs, ctx_chunks, ctx_latency_ms, ctx_embedding_ms, ctx_search_ms
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone

from app.rag.schemas import Document, ContextPackage, IngestStats, AnswerResult
from app.rag.exceptions import PipelineError, LoaderError, EmbeddingError, VectorStoreError
from app.rag.loader import DocumentLoaderRegistry, loader_registry as _default_loader_registry
from app.rag.chunker import BaseChunker, SemanticChunker
from app.rag.embeddings import EmbeddingService, get_embedding_service
from app.rag.vector_store import VectorStore
from app.rag.retriever import RetrievalPipeline, RetrievedDocument
from app.rag.generator import AnswerGenerator, get_generator
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


class KnowledgePipeline:
    """
    Enterprise-grade knowledge platform composing:
        DocumentLoaderRegistry → BaseChunker → EmbeddingService
        → VectorStore → RetrievalPipeline

    Public API:
        initialize()            — pre-warm embedding model (call at startup)
        ingest()                — ingest raw text
        ingest_file()           — ingest a single file (PDF/MD/TXT/HTML/JSON/PY/…)
        ingest_directory()      — ingest all supported files in a directory
        ingest_documents()      — ingest pre-built Document objects (from indexers)
        retrieve_context()      — full staged retrieval → ContextPackage
        search()                — convenience wrapper → list[RetrievedDocument]
        store_count()           — current chunk count in the knowledge base
        delete_document()       — remove a document from the knowledge base
        reset()                 — wipe the knowledge base (test only)
    """

    def __init__(
        self,
        namespace: str = "default",
        loader_registry: DocumentLoaderRegistry | None = None,
        chunker: BaseChunker | None = None,
        embedding_service: EmbeddingService | None = None,
        vector_store: VectorStore | None = None,
        generator: AnswerGenerator | None = None,
    ) -> None:
        self._namespace = namespace
        self._loader_registry = loader_registry or _default_loader_registry
        self._chunker = chunker or SemanticChunker()
        self._embedding_service = embedding_service or get_embedding_service()
        self._vector_store = vector_store or VectorStore(namespace=namespace)
        self._generator = generator or get_generator()
        self._retrieval_pipeline = RetrievalPipeline(
            vector_store=self._vector_store,
            embedding_service=self._embedding_service,
        )
        self._initialized = False
        log.debug("KnowledgePipeline created", extra={"ctx_namespace": namespace})

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """
        Pre-warm the embedding model so the first query has no cold-start penalty.
        Call once during app startup (already wired into main.py lifespan).
        Idempotent — safe to call multiple times.
        """
        if self._initialized:
            return
        log.info("KnowledgePipeline initializing", extra={"ctx_namespace": self._namespace})
        try:
            _ = self._embedding_service.dimension   # triggers lazy model load
            self._initialized = True
            log.info(
                "KnowledgePipeline ready",
                extra={
                    "ctx_namespace":  self._namespace,
                    "ctx_model":      self._embedding_service.model_name,
                    "ctx_dim":        self._embedding_service.dimension,
                    "ctx_backend":    self._vector_store.backend_type,
                },
            )
        except Exception as exc:
            raise PipelineError(f"Pipeline initialization failed: {exc}") from exc

    # ── Ingestion ──────────────────────────────────────────────────────────────

    def ingest(
        self,
        text: str,
        source: str = "inline",
        doc_type: str = "text",
        metadata: dict | None = None,
    ) -> IngestStats:
        """
        Ingest a raw text string directly into the knowledge base.
        Use for API payloads, agent-generated text, or unit tests.

        Returns IngestStats with chunk counts and latency.
        """
        doc_id = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        doc = Document(
            id=doc_id,
            source=source,
            content=text,
            doc_type=doc_type,
            metadata=metadata or {"source": source, "doc_type": doc_type},
            ingested_at=datetime.now(timezone.utc).isoformat(),
        )
        return self._ingest_document(doc)

    def ingest_file(self, path: str, source: str | None = None) -> IngestStats:
        """
        Load and ingest a single file.
        Supported: .pdf .md .txt .html .json .py .ts .js .go .rs .yaml and more.
        Raises PipelineError on load failure.

        `source` overrides the stored source label. Callers ingesting from a
        temporary path (e.g. an upload) should pass the original filename so the
        internal path never leaks into stored metadata or API responses.
        """
        t0 = time.perf_counter()
        try:
            doc = self._loader_registry.load(path)
        except LoaderError as exc:
            log.error(
                "File ingest failed",
                extra={"ctx_path": path, "ctx_error": str(exc)},
            )
            raise PipelineError(f"File load failed: {exc}", {"path": path}) from exc
        if source:
            doc.source = source
            doc.metadata["source"] = source
        load_ms = round((time.perf_counter() - t0) * 1000, 2)
        stats = self._ingest_document(doc)
        stats.latency_ms = round(stats.latency_ms + load_ms, 2)
        return stats

    def ingest_directory(
        self,
        path: str,
        extensions: list[str] | None = None,
    ) -> IngestStats:
        """
        Recursively ingest all supported files in a directory.
        Pass extensions to restrict (e.g. [".md", ".py"]).
        Failures per-file are logged and counted; never abort the whole batch.
        """
        t0 = time.perf_counter()
        try:
            docs = self._loader_registry.load_directory(path, extensions=extensions)
        except LoaderError as exc:
            raise PipelineError(f"Directory load failed: {exc}", {"path": path}) from exc

        total = IngestStats(
            documents_processed=0, chunks_created=0, chunks_stored=0,
            failed_documents=0, latency_ms=0.0, sources=[],
        )
        for doc in docs:
            try:
                stats = self._ingest_document(doc)
                total.documents_processed += stats.documents_processed
                total.chunks_created      += stats.chunks_created
                total.chunks_stored       += stats.chunks_stored
                total.sources.extend(stats.sources)
            except PipelineError as exc:
                total.failed_documents += 1
                log.warning(
                    "Document ingest failed, continuing",
                    extra={"ctx_source": doc.source, "ctx_error": str(exc)},
                )

        total.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        log.info(
            "Directory ingested",
            extra={
                "ctx_path":     path,
                "ctx_docs":     total.documents_processed,
                "ctx_chunks":   total.chunks_stored,
                "ctx_failed":   total.failed_documents,
                "ctx_latency_ms": total.latency_ms,
            },
        )
        return total

    def ingest_documents(self, documents: list[Document]) -> IngestStats:
        """
        Ingest pre-built Document objects — used by GitHub Indexer, ML pipeline,
        or any external producer that already has structured documents.
        Documents may already have chunks pre-populated; if not, they are chunked here.
        """
        t0 = time.perf_counter()
        total = IngestStats(
            documents_processed=0, chunks_created=0, chunks_stored=0,
            failed_documents=0, latency_ms=0.0, sources=[],
        )
        for doc in documents:
            try:
                s = self._ingest_document(doc)
                total.documents_processed += s.documents_processed
                total.chunks_created      += s.chunks_created
                total.chunks_stored       += s.chunks_stored
                total.sources.extend(s.sources)
            except PipelineError as exc:
                total.failed_documents += 1
                log.warning(
                    "Document ingest failed",
                    extra={"ctx_doc_id": doc.id, "ctx_error": str(exc)},
                )
        total.latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        log.info(
            "Batch ingested",
            extra={
                "ctx_total_docs":   len(documents),
                "ctx_chunks":       total.chunks_stored,
                "ctx_failed":       total.failed_documents,
                "ctx_latency_ms":   total.latency_ms,
            },
        )
        return total

    # ── Query / Retrieval ──────────────────────────────────────────────────────

    def retrieve_context(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
        post_filters: dict | None = None,
    ) -> ContextPackage:
        """
        Full retrieval pipeline: embed → search → filter → rank → assemble.
        Returns a ContextPackage. Call .as_context_string() to get LLM-ready text.

        IMPORTANT: every agent should call this method, never the vector store directly.
        The Orchestrator may call this before invoking an agent to pre-load context.

        Args:
            query:        natural-language question or task description
            top_k:        number of results (default: config.rag_top_k)
            filters:      backend-level WHERE filter dict (ChromaDB / Qdrant)
            post_filters: Python-side metadata filter applied after search
        """
        if not query or not query.strip():
            raise PipelineError("Query cannot be empty.")
        try:
            return self._retrieval_pipeline.retrieve_context(
                query=query,
                top_k=top_k,
                filters=filters,
                post_filters=post_filters,
            )
        except Exception as exc:
            raise PipelineError(
                f"Context retrieval failed: {exc}",
                {"query": query[:80]},
            ) from exc

    def search(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
    ) -> list[RetrievedDocument]:
        """
        Convenience wrapper: retrieve_context() → list[RetrievedDocument].
        Use when you need a ranked list without the full ContextPackage envelope.
        """
        pkg = self.retrieve_context(query=query, top_k=top_k, filters=filters)
        return [
            RetrievedDocument(
                text=r.text,
                score=r.score,
                metadata=r.metadata,
                rank=r.rank,
            )
            for r in pkg.results
        ]

    def answer(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
    ) -> AnswerResult:
        """
        Full RAG: retrieve context, then generate a grounded answer with inline
        citations. This is the "G" in RAG — retrieve_context() only returns
        chunks; answer() synthesizes a response traceable to its sources.

        Uses the injected AnswerGenerator (offline ExtractiveGenerator by
        default; OpenAIGenerator when OPENAI_API_KEY is configured).
        """
        if not query or not query.strip():
            raise PipelineError("Query cannot be empty.")
        pkg = self.retrieve_context(query=query, top_k=top_k, filters=filters)
        try:
            return self._generator.generate(query, pkg)
        except Exception as exc:
            raise PipelineError(
                f"Answer generation failed: {exc}",
                {"query": query[:80]},
            ) from exc

    # ── Knowledge base management ──────────────────────────────────────────────

    def store_count(self) -> int:
        """Total number of chunks currently in the knowledge base."""
        return self._vector_store.count()

    def delete_document(self, doc_id: str) -> None:
        """Remove all chunks for a given document id from the knowledge base."""
        self._vector_store.delete_document(doc_id)
        log.info("Document deleted from knowledge base", extra={"ctx_doc_id": doc_id})

    def reset(self) -> None:
        """
        Wipe the entire knowledge base.
        DESTRUCTIVE — use only in tests or dev environments.
        """
        self._vector_store.reset()
        log.warning(
            "KnowledgePipeline store reset",
            extra={"ctx_namespace": self._namespace},
        )

    # ── Internal ───────────────────────────────────────────────────────────────

    def _ingest_document(self, doc: Document) -> IngestStats:
        """
        Core ingestion flow:
          1. Chunk document if not already chunked
          2. Embed chunk texts
          3. Store chunks + embeddings in vector store
          4. Return IngestStats
        """
        t0 = time.perf_counter()

        # ── Chunking ──────────────────────────────────────────────────────────
        if not doc.chunks:
            doc.chunks = self._chunker.chunk(
                doc.content,
                metadata={
                    "doc_id":   doc.id,
                    "source":   doc.source,
                    "doc_type": doc.doc_type,
                    "language": doc.language,
                },
            )

        if not doc.chunks:
            log.warning(
                "Document produced no chunks — skipping",
                extra={"ctx_source": doc.source, "ctx_doc_id": doc.id},
            )
            return IngestStats(
                documents_processed=1, chunks_created=0, chunks_stored=0,
                failed_documents=0, latency_ms=0.0, sources=[doc.source],
            )

        # ── Embedding ──────────────────────────────────────────────────────────
        chunk_texts = [c.text for c in doc.chunks]
        try:
            embedding_result = self._embedding_service.embed(chunk_texts)
            embeddings = embedding_result.vectors
        except EmbeddingError as exc:
            raise PipelineError(
                f"Embedding failed during ingest: {exc}",
                {"doc_id": doc.id, "source": doc.source},
            ) from exc

        # ── Storage ────────────────────────────────────────────────────────────
        try:
            self._vector_store.add_chunks(
                chunks=doc.chunks,
                doc_id=doc.id,
                embeddings=embeddings,
                extra_metadata={"source": doc.source, "doc_type": doc.doc_type},
            )
        except VectorStoreError as exc:
            raise PipelineError(
                f"Storage failed during ingest: {exc}",
                {"doc_id": doc.id},
            ) from exc

        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        log.info(
            "Document ingested",
            extra={
                "ctx_doc_id":       doc.id,
                "ctx_source":       doc.source,
                "ctx_chunks":       len(doc.chunks),
                "ctx_latency_ms":   latency_ms,
                "ctx_embedding_ms": embedding_result.latency_ms,
            },
        )
        return IngestStats(
            documents_processed=1,
            chunks_created=len(doc.chunks),
            chunks_stored=len(doc.chunks),
            failed_documents=0,
            latency_ms=latency_ms,
            sources=[doc.source],
        )
