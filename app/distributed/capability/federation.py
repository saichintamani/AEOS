"""
Wave 9B.5.8 — Capability Federation

Workers dynamically advertise rich capability profiles. The coordinator
discovers them without prior configuration.

Capability categories:
  LLM       — language model inference (model names + context length)
  GPU       — GPU compute (VRAM, CUDA version)
  Vision    — image/video processing
  Memory    — vector store / long-term memory access
  Search    — web / document search
  OCR       — optical character recognition
  Embedding — embedding generation (model + dimension)
  RAG       — retrieval-augmented generation
  ToolCall  — function/tool calling support
  Code      — code execution / sandboxing
  Planning  — multi-step planning / agent orchestration

Architecture:
  CapabilityAdvertisement  — typed advertisement a worker broadcasts
  CapabilityRegistry       — coordinator-side index (in-memory, refreshed by heartbeat)
  CapabilityFederator      — high-level API: advertise() / discover() / match()
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements

logger = logging.getLogger(__name__)

_ADVERTISEMENT_TTL_S = 30.0   # stale after 30 s without refresh


# ── Capability categories ─────────────────────────────────────────────────────

class CapabilityCategory(str, Enum):
    LLM       = "llm"
    GPU       = "gpu"
    VISION    = "vision"
    MEMORY    = "memory"
    SEARCH    = "search"
    OCR       = "ocr"
    EMBEDDING = "embedding"
    RAG       = "rag"
    TOOL_CALL = "tool_call"
    CODE      = "code"
    PLANNING  = "planning"


# ── Advertisement ─────────────────────────────────────────────────────────────

@dataclass
class LLMCapability:
    models: list[str] = field(default_factory=list)
    max_context_tokens: int = 4096
    supports_function_calling: bool = False
    supports_streaming: bool = True


@dataclass
class GPUCapability:
    device_count: int = 0
    vram_gb: float = 0.0
    cuda_version: str = ""
    compute_capability: str = ""    # e.g. "8.6" (Ampere)


@dataclass
class EmbeddingCapability:
    models: list[str] = field(default_factory=list)
    max_dimensions: int = 1536
    batch_size: int = 64


@dataclass
class MemoryCapability:
    backend: str = "in_memory"      # "in_memory" | "redis" | "pgvector" | "weaviate"
    max_vectors: int = 100_000
    supports_hybrid_search: bool = False


@dataclass
class CapabilityAdvertisement:
    """
    Rich capability advertisement broadcast by a worker.

    Workers call CapabilityFederator.advertise() on startup and refresh
    every heartbeat interval. The federator marks entries stale after
    _ADVERTISEMENT_TTL_S.
    """
    worker_id: str
    host: str = ""
    port: int = 0
    region: str = "us-east-1"
    az: str = "a"

    # Core resource metrics
    cpu_cores: int = 1
    memory_gb: float = 1.0
    current_load: float = 0.0       # 0–1
    queue_depth: int = 0

    # Capability modules — None = not present
    llm: LLMCapability | None = None
    gpu: GPUCapability | None = None
    embedding: EmbeddingCapability | None = None
    memory_store: MemoryCapability | None = None

    # Generic flags for lightweight categories
    has_vision: bool = False
    has_search: bool = False
    has_ocr: bool = False
    has_rag: bool = False
    has_tool_call: bool = False
    has_code_execution: bool = False
    has_planning: bool = False

    # Additional metadata
    skills: frozenset[str] = field(default_factory=frozenset)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Set by the federator on receipt
    advertised_at: float = field(default_factory=time.monotonic)

    @property
    def categories(self) -> set[CapabilityCategory]:
        cats: set[CapabilityCategory] = set()
        if self.llm:
            cats.add(CapabilityCategory.LLM)
        if self.gpu and self.gpu.device_count > 0:
            cats.add(CapabilityCategory.GPU)
        if self.embedding:
            cats.add(CapabilityCategory.EMBEDDING)
        if self.memory_store:
            cats.add(CapabilityCategory.MEMORY)
        if self.has_vision:
            cats.add(CapabilityCategory.VISION)
        if self.has_search:
            cats.add(CapabilityCategory.SEARCH)
        if self.has_ocr:
            cats.add(CapabilityCategory.OCR)
        if self.has_rag:
            cats.add(CapabilityCategory.RAG)
        if self.has_tool_call:
            cats.add(CapabilityCategory.TOOL_CALL)
        if self.has_code_execution:
            cats.add(CapabilityCategory.CODE)
        if self.has_planning:
            cats.add(CapabilityCategory.PLANNING)
        return cats

    def to_capability_profile(self) -> CapabilityProfile:
        """Convert to the IOC CapabilityProfile used by the decision engine."""
        models = (self.llm.models if self.llm else []) + (
            self.embedding.models if self.embedding else []
        )
        return CapabilityProfile(
            worker_id=self.worker_id,
            memory_gb=self.memory_gb,
            gpu_available=bool(self.gpu and self.gpu.device_count > 0),
            gpu_memory_gb=self.gpu.vram_gb if self.gpu else 0.0,
            cpu_cores=self.cpu_cores,
            supported_models=models,
            skills=frozenset(cat.value for cat in self.categories) | self.skills,
            current_load=self.current_load,
            queue_depth=self.queue_depth,
            region=self.region,
            az=self.az,
        )


# ── Registry ──────────────────────────────────────────────────────────────────

class CapabilityRegistry:
    """
    In-memory index of worker advertisements, refreshed by heartbeat.

    TTL-based eviction: entries not refreshed within _ADVERTISEMENT_TTL_S
    are considered stale and excluded from discovery queries.
    """

    def __init__(self, ttl_s: float = _ADVERTISEMENT_TTL_S) -> None:
        self._ttl = ttl_s
        self._store: dict[str, CapabilityAdvertisement] = {}
        self._lock = asyncio.Lock()

    async def put(self, adv: CapabilityAdvertisement) -> None:
        adv.advertised_at = time.monotonic()
        async with self._lock:
            self._store[adv.worker_id] = adv

    async def remove(self, worker_id: str) -> None:
        async with self._lock:
            self._store.pop(worker_id, None)

    async def get(self, worker_id: str) -> CapabilityAdvertisement | None:
        async with self._lock:
            adv = self._store.get(worker_id)
        if adv and self._is_fresh(adv):
            return adv
        return None

    async def all_fresh(self) -> list[CapabilityAdvertisement]:
        now = time.monotonic()
        async with self._lock:
            return [a for a in self._store.values() if (now - a.advertised_at) < self._ttl]

    async def by_category(
        self, category: CapabilityCategory
    ) -> list[CapabilityAdvertisement]:
        fresh = await self.all_fresh()
        return [a for a in fresh if category in a.categories]

    async def evict_stale(self) -> int:
        now = time.monotonic()
        async with self._lock:
            stale = [wid for wid, a in self._store.items()
                     if (now - a.advertised_at) >= self._ttl]
            for wid in stale:
                del self._store[wid]
        if stale:
            logger.info("CapabilityRegistry: evicted %d stale workers: %s", len(stale), stale)
        return len(stale)

    @property
    def worker_count(self) -> int:
        return len(self._store)

    def _is_fresh(self, adv: CapabilityAdvertisement) -> bool:
        return (time.monotonic() - adv.advertised_at) < self._ttl


# ── Federator ─────────────────────────────────────────────────────────────────

@dataclass
class FederationMatch:
    """Result of a federation discovery query."""
    worker_id: str
    advertisement: CapabilityAdvertisement
    profile: CapabilityProfile
    match_score: float          # 0–1
    matched_categories: set[CapabilityCategory] = field(default_factory=set)
    matched_models: list[str] = field(default_factory=list)


class CapabilityFederator:
    """
    High-level federation API for the coordinator.

    advertise(adv)   — called by workers (or their proxy) to register capabilities
    discover(cats)   — find all workers with the requested capability categories
    match(req)       — match a TaskRequirements against registered workers,
                       returning ranked FederationMatch objects
    profiles()       — return all fresh CapabilityProfiles for the decision engine
    """

    def __init__(self, ttl_s: float = _ADVERTISEMENT_TTL_S) -> None:
        self._registry = CapabilityRegistry(ttl_s)

    async def advertise(self, adv: CapabilityAdvertisement) -> None:
        await self._registry.put(adv)
        logger.debug(
            "CapabilityFederator: worker %s advertised categories=%s",
            adv.worker_id, {c.value for c in adv.categories},
        )

    async def withdraw(self, worker_id: str) -> None:
        await self._registry.remove(worker_id)
        logger.info("CapabilityFederator: worker %s withdrew", worker_id)

    async def discover(
        self,
        categories: set[CapabilityCategory] | None = None,
        *,
        require_models: list[str] | None = None,
    ) -> list[CapabilityAdvertisement]:
        """Return fresh workers matching all requested categories and models."""
        candidates = await self._registry.all_fresh()

        if categories:
            candidates = [c for c in candidates if categories.issubset(c.categories)]

        if require_models:
            def has_models(adv: CapabilityAdvertisement) -> bool:
                available = set(adv.llm.models if adv.llm else []) | set(
                    adv.embedding.models if adv.embedding else []
                )
                return all(m in available for m in require_models)
            candidates = [c for c in candidates if has_models(c)]

        return candidates

    async def match(self, requirements: TaskRequirements) -> list[FederationMatch]:
        """
        Match a task requirement against registered workers.

        Scoring (0–1):
          0.40 × capability coverage (fraction of required skills met)
          0.20 × model match (1.0 if all required models present)
          0.20 × load headroom (1 - current_load)
          0.10 × GPU match (binary if requires_gpu)
          0.10 × memory headroom (capped fit)
        """
        candidates = await self._registry.all_fresh()
        matches = []
        for adv in candidates:
            score, cats, models = self._score(adv, requirements)
            if score > 0.0:
                matches.append(FederationMatch(
                    worker_id=adv.worker_id,
                    advertisement=adv,
                    profile=adv.to_capability_profile(),
                    match_score=score,
                    matched_categories=cats,
                    matched_models=models,
                ))
        matches.sort(key=lambda m: m.match_score, reverse=True)
        return matches

    async def profiles(self) -> list[CapabilityProfile]:
        """Return CapabilityProfile for every fresh worker — for the decision engine."""
        fresh = await self._registry.all_fresh()
        return [a.to_capability_profile() for a in fresh]

    async def evict_stale(self) -> int:
        return await self._registry.evict_stale()

    @property
    def registry(self) -> CapabilityRegistry:
        return self._registry

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score(
        self,
        adv: CapabilityAdvertisement,
        req: TaskRequirements,
    ) -> tuple[float, set[CapabilityCategory], list[str]]:
        cats = adv.categories

        # GPU hard constraint
        if req.requires_gpu and CapabilityCategory.GPU not in cats:
            return 0.0, set(), []

        # Memory hard constraint
        if req.required_memory_gb > 0 and adv.memory_gb < req.required_memory_gb:
            return 0.0, set(), []

        # Skill coverage
        skill_score = 0.0
        if req.required_skills:
            matched_skills = req.required_skills & (adv.skills | frozenset(c.value for c in cats))
            skill_score = len(matched_skills) / len(req.required_skills)
        else:
            skill_score = 1.0

        # Model match
        available_models = set(adv.llm.models if adv.llm else []) | set(
            adv.embedding.models if adv.embedding else []
        )
        matched_models: list[str] = []
        model_score = 1.0
        if req.required_models:
            matched_models = [m for m in req.required_models if m in available_models]
            model_score = len(matched_models) / len(req.required_models)
            if model_score == 0.0:
                return 0.0, set(), []

        # Load headroom
        load_score = max(0.0, 1.0 - adv.current_load)

        # GPU match bonus (not hard constraint here — already handled above)
        gpu_score = 1.0 if not req.requires_gpu else (
            1.0 if adv.gpu and adv.gpu.vram_gb >= req.required_gpu_memory_gb else 0.5
        )

        # Memory headroom
        mem_score = 1.0
        if req.required_memory_gb > 0 and adv.memory_gb > 0:
            headroom = adv.memory_gb - req.required_memory_gb
            mem_score = min(headroom / adv.memory_gb, 1.0)

        total = (
            0.40 * skill_score
            + 0.20 * model_score
            + 0.20 * load_score
            + 0.10 * gpu_score
            + 0.10 * mem_score
        )

        matched_cats = cats & frozenset(
            CapabilityCategory(s) for s in req.required_skills
            if s in CapabilityCategory._value2member_map_
        )

        return total, matched_cats, matched_models
