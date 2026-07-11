"""
AEOS RAG — Staged Retrieval Pipeline
Each stage is independently replaceable by subclassing RetrievalStage.

Pipeline:
    Query string
        → [EmbeddingStage]  embed query, record latency
        → [SearchStage]     cosine similarity search (over-fetch × 3 for rerank)
        → [FilterStage]     Python-side metadata post-filtering (optional)
        → [RankingStage]    BM25 keyword reranking, weighted hybrid score
        → [AssemblyStage]   build ContextPackage with provenance + metrics
        → ContextPackage

Usage:
    pipeline = RetrievalPipeline(vector_store=store, embedding_service=svc)
    context  = pipeline.retrieve_context("What is AEOS?", top_k=5)
    text     = context.as_context_string()   # inject into LLM prompt

Backward compatibility:
    HybridRetriever = RetrievalPipeline   (alias for old code)
    RetrievedDocument                     (legacy dataclass, kept for main.py routes)
"""
from __future__ import annotations

import math
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.rag.schemas import ContextPackage, RetrievalResult, SearchResult
from app.rag.exceptions import RetrievalError
from app.rag.vector_store import VectorStore
from app.rag.embeddings import EmbeddingService, get_embedding_service
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


# ── Stage contract ─────────────────────────────────────────────────────────────

class RetrievalStage(ABC):
    """
    Abstract base for each stage in the retrieval pipeline.
    Stages communicate via a shared state dict — this avoids coupling
    stage A's output type to stage B's input type.
    """

    @abstractmethod
    def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """
        Consume state, produce updated state.
        Raise RetrievalError on unrecoverable failure.
        """
        ...


# ── Stage 1: Embedding ─────────────────────────────────────────────────────────

class EmbeddingStage(RetrievalStage):
    """
    Embeds the query string using the injected EmbeddingService.
    Writes: state["query_embedding"], state["embedding_latency_ms"]
    """

    def __init__(self, embedding_service: EmbeddingService) -> None:
        self._service = embedding_service

    def process(self, state: dict) -> dict:
        query: str = state["query"]
        t0 = time.perf_counter()
        try:
            state["query_embedding"] = self._service.embed_one(query)
        except Exception as exc:
            raise RetrievalError(f"Query embedding failed: {exc}", {"query": query[:80]}) from exc
        state["embedding_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)
        log.debug(
            "EmbeddingStage",
            extra={"ctx_latency_ms": state["embedding_latency_ms"]},
        )
        return state


# ── Stage 2: Vector Search ─────────────────────────────────────────────────────

class SearchStage(RetrievalStage):
    """
    Runs approximate nearest-neighbour search against the vector store.
    Over-fetches by a factor to give the RankingStage enough candidates.
    Writes: state["candidates"] (list[SearchResult]), state["search_latency_ms"]
    """

    def __init__(self, vector_store: VectorStore, over_fetch_factor: int = 3) -> None:
        self._store = vector_store
        self._over_fetch = over_fetch_factor

    def process(self, state: dict) -> dict:
        embedding: list[float] = state["query_embedding"]
        top_k: int = state["top_k"]
        filters: dict | None = state.get("filters")

        fetch_k = min(top_k * self._over_fetch, max(1, self._store.count()))
        t0 = time.perf_counter()
        try:
            raw = self._store.query(
                query_embedding=embedding,
                top_k=fetch_k,
                where=filters,
            )
        except Exception as exc:
            raise RetrievalError(f"Vector search failed: {exc}") from exc
        state["search_latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)

        state["candidates"] = [
            SearchResult(
                id=r["id"],
                text=r["text"],
                score=r["score"],
                metadata=r["metadata"],
                rank=i + 1,
            )
            for i, r in enumerate(raw)
        ]
        log.debug(
            "SearchStage",
            extra={
                "ctx_candidates": len(state["candidates"]),
                "ctx_latency_ms": state["search_latency_ms"],
            },
        )
        return state


# ── Stage 3: Metadata Filtering ────────────────────────────────────────────────

class FilterStage(RetrievalStage):
    """
    Optional Python-side post-filter on metadata fields.
    Useful for backends that don't support server-side WHERE filtering,
    or for additional filter predicates beyond what the backend supports.
    No-op when state["post_filters"] is None or empty.
    Reads: state["post_filters"] (dict)
    Writes: state["candidates"] (filtered in-place)
    """

    def process(self, state: dict) -> dict:
        post_filters: dict | None = state.get("post_filters")
        if not post_filters:
            return state
        before = len(state["candidates"])
        state["candidates"] = [
            c for c in state["candidates"]
            if all(c.metadata.get(k) == v for k, v in post_filters.items())
        ]
        log.debug(
            "FilterStage",
            extra={"ctx_before": before, "ctx_after": len(state["candidates"])},
        )
        return state


# ── Stage 4: BM25 Reranking ────────────────────────────────────────────────────

class RankingStage(RetrievalStage):
    """
    Hybrid reranking over dense candidates using BM25 keyword scoring.
    combined_score = α × dense_score + (1−α) × bm25_score
    Default α = 0.7 (dense-heavy; adjust for keyword-heavy domains).
    Reads:  state["candidates"], state["query"], state["top_k"]
    Writes: state["ranked"]  list[(combined_score, SearchResult)]
    """

    def __init__(self, dense_weight: float = 0.7) -> None:
        self._alpha = max(0.0, min(1.0, dense_weight))

    def process(self, state: dict) -> dict:
        query: str = state["query"]
        candidates: list[SearchResult] = state["candidates"]
        top_k: int = state["top_k"]

        if not candidates:
            state["ranked"] = []
            return state

        query_terms = self._tokenise(query)
        corpus = [c.text for c in candidates]
        idf = self._compute_idf(query_terms, corpus)

        scored: list[tuple[float, SearchResult]] = []
        for c in candidates:
            bm25 = self._bm25_score(query_terms, c.text, idf, corpus)
            combined = self._alpha * c.score + (1 - self._alpha) * bm25
            scored.append((combined, c))

        scored.sort(key=lambda x: x[0], reverse=True)
        state["ranked"] = scored[:top_k]
        log.debug("RankingStage", extra={"ctx_ranked": len(state["ranked"])})
        return state

    # ── BM25 implementation ────────────────────────────────────────────────────

    def _bm25_score(
        self,
        query_terms: list[str],
        text: str,
        idf: dict[str, float],
        corpus: list[str],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> float:
        doc_terms = self._tokenise(text)
        doc_len = len(doc_terms)
        avg_len = sum(len(self._tokenise(d)) for d in corpus) / max(len(corpus), 1)
        tf_map: dict[str, int] = {}
        for term in doc_terms:
            tf_map[term] = tf_map.get(term, 0) + 1

        score = 0.0
        for term in query_terms:
            if term not in tf_map:
                continue
            tf = tf_map[term]
            numerator   = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * doc_len / max(avg_len, 1))
            score += idf.get(term, 0.0) * (numerator / denominator)

        max_possible = len(query_terms) * (k1 + 1)
        return min(score / max_possible, 1.0) if max_possible > 0 else 0.0

    def _compute_idf(self, terms: list[str], corpus: list[str]) -> dict[str, float]:
        N = len(corpus)
        idf: dict[str, float] = {}
        for term in set(terms):
            df = sum(1 for doc in corpus if term in self._tokenise(doc))
            idf[term] = math.log((N - df + 0.5) / (df + 0.5) + 1)
        return idf

    @staticmethod
    def _tokenise(text: str) -> list[str]:
        return re.findall(r'\b[a-z0-9]+\b', text.lower())


# ── Stage 5: Context Assembly ──────────────────────────────────────────────────

class AssemblyStage(RetrievalStage):
    """
    Converts ranked (score, SearchResult) tuples into a ContextPackage.
    Estimates token counts, assembles source provenance, records pipeline metrics.
    Reads:  state["ranked"], state["query"], state["*_latency_ms"]
    Writes: state["context_package"]
    """

    def process(self, state: dict) -> dict:
        ranked: list[tuple[float, SearchResult]] = state.get("ranked", [])
        query: str = state["query"]

        results: list[RetrievalResult] = []
        total_tokens = 0

        for rank, (score, c) in enumerate(ranked, start=1):
            meta = c.metadata
            # Use stored token_count when available; else estimate from char length
            total_tokens += int(meta.get("token_count") or len(c.text) // 4)
            results.append(
                RetrievalResult(
                    text=c.text,
                    score=round(score, 4),
                    rank=rank,
                    source=meta.get("source", ""),
                    doc_id=meta.get("doc_id", ""),
                    chunk_index=int(meta.get("chunk_index", -1)),
                    metadata=meta,
                )
            )

        emb_ms = state.get("embedding_latency_ms", 0.0)
        search_ms = state.get("search_latency_ms", 0.0)

        state["context_package"] = ContextPackage(
            query=query,
            results=results,
            total_retrieved=len(results),
            total_tokens=total_tokens,
            retrieval_latency_ms=round(emb_ms + search_ms, 2),
            embedding_latency_ms=emb_ms,
            search_latency_ms=search_ms,
        )
        log.debug("AssemblyStage", extra={"ctx_results": len(results)})
        return state


# ── RetrievalPipeline ──────────────────────────────────────────────────────────

class RetrievalPipeline:
    """
    Composes all retrieval stages into a single retrieve_context() call.
    Stage order: Embedding → Search → Filter → Ranking → Assembly.

    Each stage is independently replaceable:
        custom = RetrievalPipeline(vector_store=store)
        custom._stages[3] = MyCustomRanker()   # swap the ranking stage

    Args:
        vector_store:      VectorStore instance to search against
        embedding_service: EmbeddingService for query embedding (default: shared singleton)
        dense_weight:      α in combined score (0.0 = pure BM25, 1.0 = pure dense)
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedding_service: EmbeddingService | None = None,
        dense_weight: float = 0.7,
    ) -> None:
        service = embedding_service or get_embedding_service()
        self._stages: list[RetrievalStage] = [
            EmbeddingStage(service),
            SearchStage(vector_store),
            FilterStage(),
            RankingStage(dense_weight=dense_weight),
            AssemblyStage(),
        ]

    def retrieve_context(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
        post_filters: dict | None = None,
    ) -> ContextPackage:
        """
        Execute the full staged retrieval pipeline.
        Returns a ContextPackage ready for LLM prompt injection.
        Raises RetrievalError if any stage fails.
        """
        k = top_k or settings.rag_top_k
        state: dict[str, Any] = {
            "query":       query,
            "top_k":       k,
            "filters":     filters,
            "post_filters": post_filters,
        }
        t0 = time.perf_counter()
        try:
            for stage in self._stages:
                state = stage.process(state)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"Retrieval pipeline failed: {exc}",
                {"query": query[:80]},
            ) from exc

        pkg: ContextPackage = state["context_package"]
        total_ms = round((time.perf_counter() - t0) * 1000, 2)
        log.info(
            "Retrieval complete",
            extra={
                "ctx_query":        query[:80],
                "ctx_results":      pkg.total_retrieved,
                "ctx_latency_ms":   total_ms,
                "ctx_embedding_ms": pkg.embedding_latency_ms,
                "ctx_search_ms":    pkg.search_latency_ms,
            },
        )
        return pkg

    def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
    ) -> list["RetrievedDocument"]:
        """
        Backward-compatible method returning list[RetrievedDocument].
        New code should call retrieve_context() for the full ContextPackage.
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


# ── Backward-compatibility shims ───────────────────────────────────────────────

@dataclass
class RetrievedDocument:
    """
    Legacy result dataclass preserved for backward compatibility.
    Used by main.py /rag/query route and any existing agent code.
    New code should use RetrievalResult / ContextPackage from schemas.py.
    """
    text: str
    score: float
    metadata: dict
    rank: int


# Alias — old code that imports HybridRetriever continues to work unchanged
HybridRetriever = RetrievalPipeline
