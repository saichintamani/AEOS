"""
Wave 9B.3.1 — Capability Graph

Indexes CapabilityProfiles for O(1) lookup by worker_id and O(n) scan for
capability matching. Updated on every worker heartbeat.

The graph answers queries like:
  "Which workers support model X, have GPU, and trust_score > 0.8?"
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Iterable

from app.runtime_intelligence.contracts import CapabilityProfile

logger = logging.getLogger(__name__)


class CapabilityGraph:
    """
    In-memory index of worker capability profiles.

    Maintains reverse indexes by:
      - skill
      - supported model
      - supported agent
      - region

    All mutations are asyncio-lock protected.
    """

    def __init__(self) -> None:
        self._profiles: dict[str, CapabilityProfile] = {}
        self._skill_index: dict[str, set[str]] = defaultdict(set)      # skill → {worker_id}
        self._model_index: dict[str, set[str]] = defaultdict(set)      # model → {worker_id}
        self._agent_index: dict[str, set[str]] = defaultdict(set)      # agent → {worker_id}
        self._region_index: dict[str, set[str]] = defaultdict(set)     # region → {worker_id}
        self._lock = asyncio.Lock()

    async def upsert(self, profile: CapabilityProfile) -> None:
        """Insert or update a worker's capability profile."""
        async with self._lock:
            # Remove old index entries if updating
            old = self._profiles.get(profile.worker_id)
            if old:
                self._remove_indexes(old)
            self._profiles[profile.worker_id] = profile
            self._add_indexes(profile)
            logger.debug("CapabilityGraph: upserted %s", profile.worker_id)

    async def remove(self, worker_id: str) -> None:
        """Remove a worker from the graph."""
        async with self._lock:
            old = self._profiles.pop(worker_id, None)
            if old:
                self._remove_indexes(old)

    async def get(self, worker_id: str) -> CapabilityProfile | None:
        async with self._lock:
            return self._profiles.get(worker_id)

    async def all_profiles(self) -> list[CapabilityProfile]:
        async with self._lock:
            return list(self._profiles.values())

    async def healthy_profiles(self) -> list[CapabilityProfile]:
        async with self._lock:
            return [p for p in self._profiles.values() if p.is_healthy]

    async def by_skill(self, skill: str) -> list[CapabilityProfile]:
        async with self._lock:
            ids = self._skill_index.get(skill, set())
            return [self._profiles[wid] for wid in ids if wid in self._profiles]

    async def by_model(self, model: str) -> list[CapabilityProfile]:
        async with self._lock:
            ids = self._model_index.get(model, set())
            return [self._profiles[wid] for wid in ids if wid in self._profiles]

    async def by_region(self, region: str) -> list[CapabilityProfile]:
        async with self._lock:
            ids = self._region_index.get(region, set())
            return [self._profiles[wid] for wid in ids if wid in self._profiles]

    async def query(
        self,
        *,
        skills: Iterable[str] | None = None,
        models: Iterable[str] | None = None,
        requires_gpu: bool = False,
        min_memory_gb: float = 0.0,
        min_trust_score: float = 0.0,
        min_health_score: float = 0.0,
        region: str = "",
    ) -> list[CapabilityProfile]:
        """
        Multi-filter query over the capability graph.
        Returns all profiles satisfying ALL specified constraints.
        """
        async with self._lock:
            candidates = set(self._profiles.keys())

            if skills:
                for skill in skills:
                    candidates &= self._skill_index.get(skill, set())

            if models:
                for model in models:
                    candidates &= self._model_index.get(model, set())

            if region:
                candidates &= self._region_index.get(region, set())

            result = []
            for wid in candidates:
                p = self._profiles.get(wid)
                if p is None:
                    continue
                if requires_gpu and not p.gpu_available:
                    continue
                if p.memory_gb < min_memory_gb:
                    continue
                if p.trust_score < min_trust_score:
                    continue
                if p.health_score < min_health_score:
                    continue
                result.append(p)

            return result

    async def count(self) -> int:
        async with self._lock:
            return len(self._profiles)

    # ── Internal index helpers ─────────────────────────────────────────────────

    def _add_indexes(self, p: CapabilityProfile) -> None:
        for skill in p.skills:
            self._skill_index[skill].add(p.worker_id)
        for model in p.supported_models:
            self._model_index[model].add(p.worker_id)
        for agent in p.supported_agents:
            self._agent_index[agent].add(p.worker_id)
        if p.region:
            self._region_index[p.region].add(p.worker_id)

    def _remove_indexes(self, p: CapabilityProfile) -> None:
        for skill in p.skills:
            self._skill_index[skill].discard(p.worker_id)
        for model in p.supported_models:
            self._model_index[model].discard(p.worker_id)
        for agent in p.supported_agents:
            self._agent_index[agent].discard(p.worker_id)
        if p.region:
            self._region_index[p.region].discard(p.worker_id)
