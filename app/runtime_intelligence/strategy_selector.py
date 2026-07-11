"""
Wave 9B.3.4 — Strategy Selector

Chooses the best scheduling strategy for a given task at runtime.

StrategyProfile  — describes when a strategy applies
StrategySelector — evaluates profiles and picks the best strategy name
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import TaskRequirements

logger = logging.getLogger(__name__)


@dataclass
class StrategyProfile:
    name: str
    priority: int = 0            # higher = evaluated first
    applies_to_gpu: bool = False
    applies_to_latency_sensitive: bool = False   # max_latency_ms < 500
    applies_to_cost_sensitive: bool = False      # max_cost < 1.0
    applies_to_batch: bool = False
    applies_to_affinity: bool = False


_DEFAULT_PROFILES: list[StrategyProfile] = [
    StrategyProfile("affinity",           priority=100, applies_to_affinity=True),
    StrategyProfile("gpu_capability",     priority=90,  applies_to_gpu=True),
    StrategyProfile("latency_optimized",  priority=80,  applies_to_latency_sensitive=True),
    StrategyProfile("cost_optimized",     priority=70,  applies_to_cost_sensitive=True),
    StrategyProfile("batch_throughput",   priority=60,  applies_to_batch=True),
    StrategyProfile("capability_aware",   priority=50),   # default
]


class StrategySelector:
    """
    Selects the highest-priority applicable strategy for a task.
    Falls back to 'capability_aware' if nothing more specific applies.
    """

    def __init__(self, profiles: list[StrategyProfile] | None = None) -> None:
        self._profiles = sorted(
            profiles or _DEFAULT_PROFILES,
            key=lambda p: p.priority,
            reverse=True,
        )

    def select(self, requirements: TaskRequirements) -> str:
        for profile in self._profiles:
            if self._matches(profile, requirements):
                logger.debug(
                    "StrategySelector: task %s → strategy '%s'",
                    requirements.task_id, profile.name,
                )
                return profile.name
        return "capability_aware"

    @staticmethod
    def _matches(profile: StrategyProfile, req: TaskRequirements) -> bool:
        if profile.applies_to_affinity and req.affinity_worker_id:
            return True
        if profile.applies_to_gpu and req.requires_gpu:
            return True
        if profile.applies_to_latency_sensitive and req.max_latency_ms < 500:
            return True
        if profile.applies_to_cost_sensitive and req.max_cost < 1.0:
            return True
        if profile.applies_to_batch and req.priority == "batch":
            return True
        # Generic fallback profiles always match
        if (not profile.applies_to_affinity and not profile.applies_to_gpu
                and not profile.applies_to_latency_sensitive
                and not profile.applies_to_cost_sensitive
                and not profile.applies_to_batch):
            return True
        return False
