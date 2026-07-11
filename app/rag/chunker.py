"""
AEOS RAG — Chunking Engine
Three independently-selectable strategies with a shared abstract base.

    BaseChunker (ABC)
    ├── FixedChunker       — fixed character-window with overlap
    │                        Best for: code, logs, CSV, structured data
    ├── RecursiveChunker   — hierarchical separator splitting
    │                        Best for: documentation, mixed prose
    └── SemanticChunker    — sentence-packing with tiktoken + sliding overlap
                             Best for: general text; default strategy

All chunkers accept metadata dict and inject full provenance into each Chunk.
Chunk dataclass is defined in schemas.py — imported and re-exported here
so existing code using `from app.rag.chunker import Chunk` continues to work.

Factory:
    chunker = get_chunker("semantic")   # or "recursive" | "fixed"
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

# Chunk lives in schemas — re-exported here for backward compatibility
from app.rag.schemas import Chunk
from app.rag.exceptions import ChunkingError
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

__all__ = ["Chunk", "BaseChunker", "FixedChunker", "RecursiveChunker", "SemanticChunker", "get_chunker"]


# ── Token counting ─────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def _get_tokeniser():
    try:
        import tiktoken
        return tiktoken.get_encoding("cl100k_base")
    except ImportError:
        return None


def _count_tokens(text: str) -> int:
    enc = _get_tokeniser()
    if enc is None:
        return len(text) // 4   # ~4 chars/token approximation
    return len(enc.encode(text))


# ── Abstract base ──────────────────────────────────────────────────────────────

class BaseChunker(ABC):
    """
    Abstract base for all chunking strategies.

    Subclasses must implement chunk(text, metadata) → list[Chunk].
    metadata is a base dict whose fields are merged into every produced Chunk.
    """

    @abstractmethod
    def chunk(self, text: str, metadata: dict | None = None) -> list[Chunk]:
        """
        Split text into chunks.
        metadata: caller-provided base metadata merged into each chunk.
        Returns empty list for blank / empty input without raising.
        """
        ...

    def _build_chunk(
        self,
        text: str,
        index: int,
        base_meta: dict,
    ) -> Chunk:
        """Construct a Chunk with full provenance from base metadata."""
        return Chunk(
            text=text,
            index=index,
            token_count=_count_tokens(text),
            doc_id=base_meta.get("doc_id", ""),
            source=base_meta.get("source", ""),
            section=base_meta.get("section", ""),
            page=base_meta.get("page", -1),
            language=base_meta.get("language", "en"),
            metadata={**base_meta, "chunk_index": index},
        )


# ── FixedChunker ───────────────────────────────────────────────────────────────

class FixedChunker(BaseChunker):
    """
    Splits text into fixed-size character windows with configurable overlap.
    Fastest strategy — ignores semantic structure.
    Suitable for: structured data, CSV rows, log lines, source code.
    """

    def __init__(self, chunk_size: int = 1000, overlap: int = 100) -> None:
        if overlap >= chunk_size:
            raise ChunkingError(
                "overlap must be < chunk_size",
                {"chunk_size": chunk_size, "overlap": overlap},
            )
        self._chunk_size = chunk_size
        self._overlap = overlap

    def chunk(self, text: str, metadata: dict | None = None) -> list[Chunk]:
        if not text or not text.strip():
            return []
        meta = metadata or {}
        chunks: list[Chunk] = []
        step = self._chunk_size - self._overlap
        chunk_num = 0
        idx = 0
        while idx < len(text):
            window = text[idx: idx + self._chunk_size]
            if window.strip():
                chunks.append(self._build_chunk(window, chunk_num, meta))
                chunk_num += 1
            idx += step
        log.debug("FixedChunker", extra={"ctx_chunks": len(chunks), "ctx_chunk_size": self._chunk_size})
        return chunks


# ── RecursiveChunker ───────────────────────────────────────────────────────────

class RecursiveChunker(BaseChunker):
    """
    Hierarchical splitting: attempts paragraph → sentence → word → character
    boundaries in order until chunks fit within chunk_size tokens.
    Produces chunks that respect natural document structure.
    Suitable for: mixed prose, long-form articles, API documentation.
    """

    _DEFAULT_SEPARATORS = ["\n\n", "\n", ". ", "! ", "? ", "; ", " ", ""]

    def __init__(
        self,
        chunk_size: int | None = None,
        overlap: int | None = None,
        separators: list[str] | None = None,
    ) -> None:
        self._chunk_size = chunk_size or settings.rag_chunk_size
        self._overlap = overlap or settings.rag_chunk_overlap
        self._separators = separators or self._DEFAULT_SEPARATORS

    def chunk(self, text: str, metadata: dict | None = None) -> list[Chunk]:
        if not text or not text.strip():
            return []
        meta = metadata or {}
        pieces = self._split_recursive(text, self._separators)
        merged = self._merge(pieces)
        chunks = [
            self._build_chunk(t, i, meta)
            for i, t in enumerate(merged)
            if t.strip()
        ]
        log.debug("RecursiveChunker", extra={"ctx_chunks": len(chunks)})
        return chunks

    def _split_recursive(self, text: str, separators: list[str]) -> list[str]:
        if not separators:
            return [text]
        sep = separators[0]
        if sep == "":
            # Character-level fallback: ~4 chars per token
            step = self._chunk_size * 4
            return [text[i: i + step] for i in range(0, len(text), step)]
        parts = text.split(sep)
        result: list[str] = []
        for part in parts:
            if not part.strip():
                continue
            if _count_tokens(part) > self._chunk_size:
                result.extend(self._split_recursive(part, separators[1:]))
            else:
                result.append(part)
        return result

    def _merge(self, pieces: list[str]) -> list[str]:
        merged: list[str] = []
        current: list[str] = []
        current_tokens = 0
        for piece in pieces:
            t = _count_tokens(piece)
            if current_tokens + t > self._chunk_size and current:
                merged.append(" ".join(current))
                tail, tail_tokens = self._tail_overlap(current)
                current, current_tokens = tail, tail_tokens
            current.append(piece)
            current_tokens += t
        if current:
            merged.append(" ".join(current))
        return merged

    def _tail_overlap(self, pieces: list[str]) -> tuple[list[str], int]:
        result: list[str] = []
        tokens = 0
        for piece in reversed(pieces):
            t = _count_tokens(piece)
            if tokens + t > self._overlap:
                break
            result.insert(0, piece)
            tokens += t
        return result, tokens


# ── SemanticChunker ────────────────────────────────────────────────────────────

class SemanticChunker(BaseChunker):
    """
    Greedy sentence-packing chunker with sliding overlap window.
    Splits on sentence-ending punctuation and paragraph breaks,
    then packs greedily up to chunk_size tokens.
    Overlap re-includes the tail sentences of the previous chunk.

    Default strategy — best general-purpose balance of context and granularity.
    """

    def __init__(
        self,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> None:
        self._chunk_size = chunk_size or settings.rag_chunk_size
        self._overlap = overlap or settings.rag_chunk_overlap

    def chunk(self, text: str, metadata: dict | None = None) -> list[Chunk]:
        if not text or not text.strip():
            return []
        meta = metadata or {}
        sentences = self._split_sentences(text)
        packed = self._pack_sentences(sentences)
        chunks = [
            self._build_chunk(t, i, meta)
            for i, t in enumerate(packed)
            if t.strip()
        ]
        log.debug("SemanticChunker", extra={"ctx_chunks": len(chunks)})
        return chunks

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """Split on sentence-ending punctuation or paragraph breaks."""
        parts = re.split(r'(?<=[.!?])\s+|\n\n+', text)
        return [p.strip() for p in parts if p.strip()]

    def _pack_sentences(self, sentences: list[str]) -> list[str]:
        result: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for sentence in sentences:
            s_tokens = _count_tokens(sentence)

            # Single oversized sentence — emit as its own chunk
            if s_tokens > self._chunk_size:
                if current:
                    result.append(" ".join(current))
                result.append(sentence)
                current, current_tokens = [], 0
                continue

            if current_tokens + s_tokens > self._chunk_size and current:
                result.append(" ".join(current))
                tail, tail_tokens = self._tail_overlap(current)
                current, current_tokens = tail, tail_tokens

            current.append(sentence)
            current_tokens += s_tokens

        if current:
            result.append(" ".join(current))
        return result

    def _tail_overlap(self, sentences: list[str]) -> tuple[list[str], int]:
        result: list[str] = []
        tokens = 0
        for sentence in reversed(sentences):
            t = _count_tokens(sentence)
            if tokens + t > self._overlap:
                break
            result.insert(0, sentence)
            tokens += t
        return result, tokens


# ── Factory ────────────────────────────────────────────────────────────────────

_STRATEGY_MAP: dict[str, type[BaseChunker]] = {
    "semantic":  SemanticChunker,
    "recursive": RecursiveChunker,
    "fixed":     FixedChunker,
}


def get_chunker(strategy: str = "semantic", **kwargs: Any) -> BaseChunker:
    """
    Factory function that returns a configured chunker instance.

    Args:
        strategy: "semantic" (default) | "recursive" | "fixed"
        **kwargs: forwarded to the chunker constructor
                  (chunk_size, overlap, separators, …)

    Raises:
        ChunkingError: if strategy is not recognised.
    """
    cls = _STRATEGY_MAP.get(strategy)
    if cls is None:
        raise ChunkingError(
            f"Unknown chunking strategy: {strategy!r}",
            {"available": list(_STRATEGY_MAP.keys())},
        )
    return cls(**kwargs)
