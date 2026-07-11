"""
Software Intelligence Platform — Code Embedding Engine
=======================================================
Generates dense vector embeddings of code entities for semantic search.

Embedding targets:
  - Functions  (signature + docstring + body summary)
  - Classes    (name + bases + method signatures)
  - Modules    (file path + top-level docstring + import list)
  - Issues     (title + body)
  - PR bodies  (title + description + file list)

Backends (pluggable):
  - SentenceTransformerBackend  — local model via sentence-transformers
  - OpenAIEmbeddingBackend      — text-embedding-3-small/large via API
  - AEOSAgentBackend            — delegates to the AEOS embedding agent

EmbeddingStore:
  - In-memory numpy matrix with linear scan (adequate for < 100k vectors)
  - TODO: replace with FAISS / ChromaDB / Qdrant for production scale
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import IssueRecord, ParseResult, PullRequestRecord, SearchResult


# ── Domain types ───────────────────────────────────────────────────────────────

@dataclass
class EmbeddedEntity:
    entity_id:   str
    entity_type: str          # "function" | "class" | "module" | "issue" | "pr"
    label:       str
    file_path:   str
    embedding:   list[float]
    metadata:    dict[str, Any] = field(default_factory=dict)


# ── Backend ABC ────────────────────────────────────────────────────────────────

class BaseEmbeddingBackend(ABC):
    """Converts text → dense float vector."""

    @property
    @abstractmethod
    def dimension(self) -> int: ...

    @abstractmethod
    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


# ── Concrete backends ──────────────────────────────────────────────────────────

class SentenceTransformerBackend(BaseEmbeddingBackend):
    """
    Uses sentence-transformers for local, offline embeddings.
    Default model: 'all-MiniLM-L6-v2' (384-dim, fast).
    For code: consider 'microsoft/codebert-base'.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Any = None

    def _load(self) -> None:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = SentenceTransformer(self._model_name)
            except ImportError:
                raise ImportError("Install sentence-transformers: pip install sentence-transformers")

    @property
    def dimension(self) -> int:
        return 384

    def embed(self, text: str) -> list[float]:
        self._load()
        return self._model.encode(text, convert_to_numpy=True).tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self._load()
        return self._model.encode(texts, convert_to_numpy=True).tolist()


class OpenAIEmbeddingBackend(BaseEmbeddingBackend):
    """Calls OpenAI text-embedding-3-small via the openai SDK."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        self._api_key = api_key
        self._model   = model

    @property
    def dimension(self) -> int:
        return 1536 if "small" in self._model else 3072

    def embed(self, text: str) -> list[float]:
        try:
            import openai
            client = openai.OpenAI(api_key=self._api_key)
            resp = client.embeddings.create(input=text[:8191], model=self._model)
            return resp.data[0].embedding
        except ImportError:
            raise ImportError("Install openai: pip install openai")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            import openai
            client = openai.OpenAI(api_key=self._api_key)
            resp = client.embeddings.create(input=[t[:8191] for t in texts], model=self._model)
            return [r.embedding for r in resp.data]
        except ImportError:
            raise ImportError("Install openai: pip install openai")


class HashEmbeddingBackend(BaseEmbeddingBackend):
    """
    Deterministic pseudo-embedding for testing / offline use.
    Produces a 64-dim float vector derived from MD5 hash of the text.
    NOT suitable for semantic search — use only for unit tests.
    """

    @property
    def dimension(self) -> int:
        return 64

    def embed(self, text: str) -> list[float]:
        digest = hashlib.md5(text.encode()).digest()  # 16 bytes
        # Repeat to fill 64 dims
        extended = (digest * 4)[:64]
        return [b / 255.0 for b in extended]


# ── Embedding store ────────────────────────────────────────────────────────────

class EmbeddingStore:
    """
    In-memory vector store with cosine similarity search.
    For production: swap with FAISS / ChromaDB / Qdrant.
    """

    def __init__(self) -> None:
        self._entities: list[EmbeddedEntity] = []

    def add(self, entity: EmbeddedEntity) -> None:
        self._entities.append(entity)

    def search(self, query_vec: list[float], top_k: int = 10) -> list[tuple[EmbeddedEntity, float]]:
        if not self._entities:
            return []
        try:
            import numpy as np
        except ImportError:
            # Fallback: pure Python dot product
            return self._search_python(query_vec, top_k)

        import numpy as np
        q = np.array(query_vec, dtype=float)
        q_norm = q / (np.linalg.norm(q) + 1e-10)
        scores = []
        for entity in self._entities:
            v = np.array(entity.embedding, dtype=float)
            v_norm = v / (np.linalg.norm(v) + 1e-10)
            scores.append(float(np.dot(q_norm, v_norm)))
        ranked = sorted(zip(self._entities, scores), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]

    def _search_python(self, q: list[float], top_k: int) -> list[tuple[EmbeddedEntity, float]]:
        def dot(a: list[float], b: list[float]) -> float:
            return sum(x * y for x, y in zip(a, b))

        def norm(v: list[float]) -> float:
            return sum(x * x for x in v) ** 0.5

        qn = norm(q)
        results = []
        for entity in self._entities:
            vn = norm(entity.embedding)
            score = dot(q, entity.embedding) / (qn * vn + 1e-10)
            results.append((entity, score))
        return sorted(results, key=lambda x: x[1], reverse=True)[:top_k]

    @property
    def size(self) -> int:
        return len(self._entities)


# ── Code embedding engine ──────────────────────────────────────────────────────

class CodeEmbeddingEngine:
    """
    Produces EmbeddedEntity records from parsed code and stores them.

    Usage:
        engine = CodeEmbeddingEngine(backend=SentenceTransformerBackend())
        engine.index_results(parse_results)
        engine.index_issues(issues)
        results = engine.search("authentication middleware", top_k=5)
    """

    def __init__(
        self,
        backend: BaseEmbeddingBackend | None = None,
        store: EmbeddingStore | None = None,
    ) -> None:
        self._backend = backend or HashEmbeddingBackend()
        self._store   = store   or EmbeddingStore()

    @classmethod
    def with_sentence_transformers(cls, model: str = "all-MiniLM-L6-v2") -> "CodeEmbeddingEngine":
        return cls(backend=SentenceTransformerBackend(model))

    @classmethod
    def with_openai(cls, api_key: str) -> "CodeEmbeddingEngine":
        return cls(backend=OpenAIEmbeddingBackend(api_key))

    # ── Indexing ───────────────────────────────────────────────────────────────

    def index_results(self, results: list[ParseResult]) -> int:
        """Index all functions, classes, and modules from parse results. Returns count added."""
        added = 0
        texts: list[str]  = []
        entities: list[EmbeddedEntity] = []

        for result in results:
            # Module entity
            module_text = f"File: {result.file_path}\n" + "\n".join(
                f"import {imp.module}" for imp in result.imports[:20]
            )
            entities.append(EmbeddedEntity(
                entity_id=f"module:{result.file_path}",
                entity_type="module",
                label=result.file_path,
                file_path=result.file_path,
                embedding=[],
                metadata={"language": result.language.value},
            ))
            texts.append(module_text)

            # Function entities
            for fn in result.functions:
                fn_text = (
                    f"def {fn.name}({', '.join(fn.parameters)}):\n"
                    f"    \"\"\"{fn.docstring or ''}\"\"\"\n"
                    f"    # calls: {', '.join(list(fn.calls)[:5])}"
                )
                entities.append(EmbeddedEntity(
                    entity_id=f"fn:{result.file_path}:{fn.name}:{fn.line_start}",
                    entity_type="function",
                    label=fn.name,
                    file_path=result.file_path,
                    embedding=[],
                    metadata={"line": fn.line_start, "cc": fn.cyclomatic_complexity},
                ))
                texts.append(fn_text)

            # Class entities
            for cls in result.classes:
                cls_text = (
                    f"class {cls.name}({', '.join(cls.bases)}):\n"
                    f"    \"\"\"{cls.docstring or ''}\"\"\"\n"
                    f"    # methods: {', '.join(cls.methods[:10])}"
                )
                entities.append(EmbeddedEntity(
                    entity_id=f"cls:{result.file_path}:{cls.name}:{cls.line_start}",
                    entity_type="class",
                    label=cls.name,
                    file_path=result.file_path,
                    embedding=[],
                    metadata={"bases": cls.bases},
                ))
                texts.append(cls_text)

        # Batch embed
        embeddings = self._backend.embed_batch(texts)
        for entity, embedding in zip(entities, embeddings):
            entity.embedding = embedding
            self._store.add(entity)
            added += 1
        return added

    def index_issues(self, issues: list[IssueRecord]) -> int:
        texts    = [f"{i.title}\n\n{i.body[:500]}" for i in issues]
        embeddings = self._backend.embed_batch(texts)
        for issue, embedding in zip(issues, embeddings):
            self._store.add(EmbeddedEntity(
                entity_id=f"issue:{issue.issue_id}",
                entity_type="issue",
                label=f"#{issue.number}: {issue.title}",
                file_path="",
                embedding=embedding,
                metadata={"state": issue.state, "labels": issue.labels},
            ))
        return len(issues)

    def index_prs(self, prs: list[PullRequestRecord]) -> int:
        texts = [f"{p.title}\n\n{p.body[:500]}\nFiles: {', '.join(p.files_changed[:10])}" for p in prs]
        embeddings = self._backend.embed_batch(texts)
        for pr, embedding in zip(prs, embeddings):
            self._store.add(EmbeddedEntity(
                entity_id=f"pr:{pr.pr_id}",
                entity_type="pr",
                label=f"PR #{pr.number}: {pr.title}",
                file_path="",
                embedding=embedding,
                metadata={"state": pr.state},
            ))
        return len(prs)

    # ── Search ──────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        entity_types: list[str] | None = None,
    ) -> list[SearchResult]:
        q_vec = self._backend.embed(query)
        raw = self._store.search(q_vec, top_k=top_k * 3)
        results = []
        for entity, score in raw:
            if entity_types and entity.entity_type not in entity_types:
                continue
            results.append(SearchResult(
                result_id=entity.entity_id,
                entity_type=entity.entity_type,
                file_path=entity.file_path,
                label=entity.label,
                score=round(score, 4),
                metadata=entity.metadata,
            ))
            if len(results) >= top_k:
                break
        return results

    @property
    def indexed_count(self) -> int:
        return self._store.size
