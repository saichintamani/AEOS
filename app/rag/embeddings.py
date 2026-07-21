"""
AEOS RAG — Embedding Service Abstraction
Provider-agnostic interface with dependency injection.

    EmbeddingService (ABC)            — stable contract for all callers
    ├── SentenceTransformerEmbeddings — local model, zero cloud cost (default)
    │     Model: all-MiniLM-L6-v2 (~90 MB, 384-dim)
    │     Requires: pip install sentence-transformers
    └── OpenAIEmbeddings              — cloud, pay-per-use
          Model: text-embedding-3-small (1536-dim, default)
          Requires: pip install openai + OPENAI_API_KEY env var

Dependency Injection pattern:
    # Default (resolved from config):
    pipeline = KnowledgePipeline()

    # Override provider without touching pipeline logic:
    pipeline = KnowledgePipeline(
        embedding_service=OpenAIEmbeddings(model_name="text-embedding-3-large")
    )

Adding a new provider:
    class MyEmbeddings(EmbeddingService):
        def embed(self, texts): ...
        def embed_one(self, text): ...
        @property def dimension(self): ...
        @property def model_name(self): ...
"""
from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from functools import lru_cache

from app.rag.schemas import EmbeddingResult
from app.rag.exceptions import EmbeddingError
from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)

# Prevent TensorFlow from loading alongside PyTorch in conda envs
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TENSORFLOW", "1")


# ── Abstract interface ─────────────────────────────────────────────────────────

class EmbeddingService(ABC):
    """
    Provider-agnostic embedding contract.
    Inject this into KnowledgePipeline and RetrievalPipeline; never instantiate
    a concrete provider directly inside those classes.
    """

    @abstractmethod
    def embed(self, texts: list[str]) -> EmbeddingResult:
        """
        Embed a batch of texts.
        Returns EmbeddingResult with vectors, model name, dimension, and latency.
        Raises EmbeddingError on any failure.
        """
        ...

    @abstractmethod
    def embed_one(self, text: str) -> list[float]:
        """Embed a single text. Convenience wrapper around embed()."""
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        """Embedding vector dimension for this model/configuration."""
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Human-readable model identifier (for logging and metadata)."""
        ...


# ── SentenceTransformer implementation ────────────────────────────────────────

class SentenceTransformerEmbeddings(EmbeddingService):
    """
    Local embeddings via sentence-transformers.
    Model is lazy-loaded on first call — zero cost at import time.
    Thread-safe after the model is loaded.

    Default model: all-MiniLM-L6-v2
        size:      ~90 MB
        dimension: 384
        speed:     ~2000 sentences/sec on CPU
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.embedding_model
        self._model = None
        self._dimension: int | None = None

    # ── Internal ───────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if self._model is not None:
            return
        log.info(
            "Loading SentenceTransformer model",
            extra={"ctx_model": self._model_name},
        )
        try:
            from sentence_transformers import SentenceTransformer
            # Offline-first: if the model is already cached locally, load it
            # without touching the network. A cold SentenceTransformer(name)
            # performs a blocking HuggingFace HEAD request that, on a restricted
            # or first-boot network, stalls ~80s in connection-reset retries
            # before falling back to the cache. Trying local-files-only first
            # keeps the common (cached) startup path fully offline and instant;
            # we only reach the downloading load when the model isn't cached yet.
            try:
                self._model = SentenceTransformer(
                    self._model_name, local_files_only=True
                )
            except Exception:
                log.info(
                    "Embedding model not cached; downloading once",
                    extra={"ctx_model": self._model_name},
                )
                self._model = SentenceTransformer(self._model_name)
            probe = self._model.encode(["probe"], convert_to_numpy=True)
            self._dimension = int(probe.shape[1])
            log.info(
                "Embedding model ready",
                extra={"ctx_model": self._model_name, "ctx_dim": self._dimension},
            )
        except ImportError:
            raise EmbeddingError(
                "sentence-transformers not installed. Run: pip install sentence-transformers"
            )
        except Exception as exc:
            raise EmbeddingError(
                f"Failed to load model '{self._model_name}': {exc}",
                {"model": self._model_name},
            ) from exc

    # ── Public API ─────────────────────────────────────────────────────────────

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(
                texts=[], vectors=[], model=self._model_name, dimension=0, latency_ms=0.0
            )
        self._load()
        t0 = time.perf_counter()
        try:
            vectors = self._model.encode(
                texts, convert_to_numpy=True, show_progress_bar=False
            ).tolist()
        except Exception as exc:
            raise EmbeddingError(
                f"Embedding batch failed: {exc}",
                {"model": self._model_name, "batch_size": len(texts)},
            ) from exc
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        log.debug(
            "Embedding batch complete",
            extra={"ctx_count": len(texts), "ctx_latency_ms": latency_ms},
        )
        return EmbeddingResult(
            texts=texts,
            vectors=vectors,
            model=self._model_name,
            dimension=self._dimension,
            latency_ms=latency_ms,
        )

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text]).vectors[0]

    @property
    def dimension(self) -> int:
        self._load()
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model_name


# ── OpenAI implementation ──────────────────────────────────────────────────────

class OpenAIEmbeddings(EmbeddingService):
    """
    Cloud embeddings via the OpenAI Embeddings API.
    Requires: pip install openai
    Requires: OPENAI_API_KEY environment variable.

    Model options (set in constructor):
        text-embedding-3-small  — 1536 dims, fast, cheap (default)
        text-embedding-3-large  — 3072 dims, highest quality
        text-embedding-ada-002  — 1536 dims, legacy
    """

    _DIMENSION_MAP: dict[str, int] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(self, model_name: str = "text-embedding-3-small") -> None:
        self._model_name = model_name
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise EmbeddingError(
                "OPENAI_API_KEY environment variable is not set.",
                {"model": self._model_name},
            )
        try:
            import openai
            self._client = openai.OpenAI(api_key=api_key)
        except ImportError:
            raise EmbeddingError(
                "openai package not installed. Run: pip install openai"
            )
        return self._client

    def embed(self, texts: list[str]) -> EmbeddingResult:
        if not texts:
            return EmbeddingResult(
                texts=[], vectors=[], model=self._model_name, dimension=0, latency_ms=0.0
            )
        client = self._get_client()
        t0 = time.perf_counter()
        try:
            response = client.embeddings.create(model=self._model_name, input=texts)
            # OpenAI returns results in input order but we sort by index to be safe
            vectors = [
                item.embedding
                for item in sorted(response.data, key=lambda x: x.index)
            ]
        except Exception as exc:
            raise EmbeddingError(
                f"OpenAI embedding failed: {exc}",
                {"model": self._model_name, "batch_size": len(texts)},
            ) from exc
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        dim = self._DIMENSION_MAP.get(self._model_name, len(vectors[0]) if vectors else 0)
        log.debug(
            "OpenAI embedding batch complete",
            extra={"ctx_count": len(texts), "ctx_latency_ms": latency_ms},
        )
        return EmbeddingResult(
            texts=texts,
            vectors=vectors,
            model=self._model_name,
            dimension=dim,
            latency_ms=latency_ms,
        )

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text]).vectors[0]

    @property
    def dimension(self) -> int:
        return self._DIMENSION_MAP.get(self._model_name, 1536)

    @property
    def model_name(self) -> str:
        return self._model_name


# ── Default singleton ──────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_embedding_service() -> EmbeddingService:
    """
    Returns the default EmbeddingService singleton (SentenceTransformerEmbeddings).
    Cached — model is shared across all callers that use the default.

    To use a different provider, inject it into KnowledgePipeline directly:
        KnowledgePipeline(embedding_service=OpenAIEmbeddings())
    """
    return SentenceTransformerEmbeddings()


# ── Backward-compatibility alias ───────────────────────────────────────────────

class EmbeddingModel(SentenceTransformerEmbeddings):
    """
    Alias preserved for backward compatibility with code that imported EmbeddingModel.
    New code should use SentenceTransformerEmbeddings or EmbeddingService directly.
    """


def get_embedder() -> "EmbeddingModel":
    """
    Backward-compatible factory for code that called get_embedder().
    Returns the shared SentenceTransformerEmbeddings singleton cast to EmbeddingModel.
    """
    svc = get_embedding_service()
    if isinstance(svc, EmbeddingModel):
        return svc
    # If a different service was injected, wrap fall-through to default
    return EmbeddingModel()
