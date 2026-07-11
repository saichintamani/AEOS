"""
AEOS RAG — Embedding Model
Wraps sentence-transformers with lazy loading so app startup is instant.
Model downloads on first embed call (~90MB for all-MiniLM-L6-v2).
"""

from __future__ import annotations
import os
from functools import lru_cache
from typing import TYPE_CHECKING

# Must be set before any transformers/sentence-transformers import
# to prevent tensorflow being loaded (protobuf conflict in conda envs)
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TENSORFLOW", "1")

from app.core.config import settings
from app.core.logger import get_logger

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

log = get_logger(__name__)


class EmbeddingModel:
    """
    Wraps a sentence-transformers model.
    Model is loaded lazily on first call to embed() — not at import time.
    """

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.embedding_model
        self._model: SentenceTransformer | None = None
        self._dimension: int | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        log.info("Loading embedding model", extra={"ctx_model": self._model_name})
        import os
        os.environ.setdefault("USE_TF", "0")
        os.environ.setdefault("TRANSFORMERS_NO_TENSORFLOW", "1")
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self._model_name)
        # Probe dimension with an empty string
        probe = self._model.encode(["probe"], convert_to_numpy=True)
        self._dimension = probe.shape[1]
        log.info(
            "Embedding model loaded",
            extra={"ctx_model": self._model_name, "ctx_dimension": self._dimension},
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings. Returns list of float vectors."""
        if not texts:
            return []
        self._load()
        vectors = self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
        return vectors.tolist()

    def embed_one(self, text: str) -> list[float]:
        """Convenience wrapper for a single string."""
        return self.embed([text])[0]

    @property
    def dimension(self) -> int:
        self._load()
        return self._dimension


@lru_cache(maxsize=1)
def get_embedder() -> EmbeddingModel:
    """Cached singleton. Called by VectorStore and Retriever."""
    return EmbeddingModel()
