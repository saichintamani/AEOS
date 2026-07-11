"""
Six scheduling strategies — Strategy Pattern, swappable at runtime.

RoundRobinStrategy      — distributes tasks evenly in round-robin order
LeastLoadedStrategy     — picks the worker with lowest load_score
CapabilityAwareStrategy — filters by required capabilities, then LeastLoaded
PriorityAwareStrategy   — score = (1 - load) * priority_weight + capability_bonus
LocalityAwareStrategy   — score = 0.6 * (1 - load) + 0.4 * locality_match
AffinityStrategy        — hard affinity/anti-affinity with LeastLoaded fallback

Contract: AC-SCHED-001
"""

from __future__ import annotations

import threading

from app.distributed.scheduler.contracts import (
    SchedulingDecision,
    SchedulingRequest,
    SchedulingStrategy,
    WorkerView,
)

_PRIORITY_WEIGHTS = {
    "critical": 4.0,
    "high":     3.0,
    "normal":   2.0,
    "low":      1.0,
    "batch":    0.5,
}


class RoundRobinStrategy(SchedulingStrategy):
    """Distribute tasks evenly across healthy workers in round-robin order."""

    def __init__(self) -> None:
        self._index = 0
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return "round-robin"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if not healthy:
            return None
        with self._lock:
            idx = self._index % len(healthy)
            self._index += 1
        w = healthy[idx]
        return SchedulingDecision(worker=w, strategy_name=self.name, score=1.0)


class LeastLoadedStrategy(SchedulingStrategy):
    """Select the worker with the lowest composite load_score."""

    @property
    def name(self) -> str:
        return "least-loaded"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if not healthy:
            return None
        w = min(healthy, key=lambda x: x.load_score)
        return SchedulingDecision(worker=w, strategy_name=self.name, score=1.0 - w.load_score)


class CapabilityAwareStrategy(SchedulingStrategy):
    """Filter by required_capabilities, then pick by load_score."""

    @property
    def name(self) -> str:
        return "capability-aware"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if request.required_capabilities:
            healthy = [w for w in healthy if request.required_capabilities <= w.capabilities]
        if not healthy:
            return None
        w = min(healthy, key=lambda x: x.load_score)
        return SchedulingDecision(worker=w, strategy_name=self.name, score=1.0 - w.load_score)


class PriorityAwareStrategy(SchedulingStrategy):
    """
    Blend load and priority to favour low-loaded workers for high-priority tasks.

    score = (1 - load_score) * priority_weight + capability_bonus
    """

    @property
    def name(self) -> str:
        return "priority-aware"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if not healthy:
            return None
        pw = _PRIORITY_WEIGHTS.get(request.priority, 2.0)
        best = None
        best_score = -1.0
        for w in healthy:
            cap_bonus = 0.2 if request.required_capabilities <= w.capabilities else 0.0
            score = (1.0 - w.load_score) * pw + cap_bonus
            if score > best_score:
                best_score = score
                best = w
        if best is None:
            return None
        return SchedulingDecision(worker=best, strategy_name=self.name, score=best_score)


class LocalityAwareStrategy(SchedulingStrategy):
    """
    Prefer workers in the same region/AZ as the requester.

    score = 0.6 * (1 - load_score) + 0.4 * locality_score
    locality_score: 1.0 same AZ, 0.5 same region, 0.0 otherwise
    """

    @property
    def name(self) -> str:
        return "locality-aware"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if not healthy:
            return None

        def locality(w: WorkerView) -> float:
            if request.preferred_az and w.az == request.preferred_az:
                return 1.0
            if request.preferred_region and w.region == request.preferred_region:
                return 0.5
            return 0.0

        best = max(healthy, key=lambda w: 0.6 * (1.0 - w.load_score) + 0.4 * locality(w))
        score = 0.6 * (1.0 - best.load_score) + 0.4 * locality(best)
        return SchedulingDecision(worker=best, strategy_name=self.name, score=score)


class AffinityStrategy(SchedulingStrategy):
    """
    Hard affinity/anti-affinity scheduling.

    affinity_node_id set   → pin to that exact node (if healthy).
    anti_affinity_node_id  → exclude that node.
    Falls back to LeastLoaded if the affinity node is unavailable.
    """

    _fallback = LeastLoadedStrategy()

    @property
    def name(self) -> str:
        return "affinity"

    def select(self, workers: list[WorkerView], request: SchedulingRequest) -> SchedulingDecision | None:
        healthy = self.filter_healthy(workers)
        if not healthy:
            return None

        # Hard anti-affinity exclusion
        if request.anti_affinity_node_id:
            healthy = [w for w in healthy if w.node_id != request.anti_affinity_node_id]

        # Hard affinity pin
        if request.affinity_node_id:
            pinned = [w for w in healthy if w.node_id == request.affinity_node_id]
            if pinned:
                return SchedulingDecision(
                    worker=pinned[0], strategy_name=self.name, score=1.0,
                    metadata={"pinned": True},
                )
            # Affinity node unavailable — fall back to LeastLoaded
            return self._fallback.select(healthy, request)

        return self._fallback.select(healthy, request)
