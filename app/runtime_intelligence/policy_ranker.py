"""
Wave 9B.3.4 — Policy Ranker

Applies governance policy constraints on top of capability scores.

Policies can:
  - Hard-exclude workers (policy violation → score = 0)
  - Apply penalty multipliers (e.g. non-preferred region)
  - Boost workers matching preferred attributes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    CapabilityScore,
    TaskRequirements,
)

logger = logging.getLogger(__name__)

PolicyFn = Callable[[CapabilityProfile, TaskRequirements], float]   # returns multiplier 0..1+


@dataclass
class PolicyRule:
    name: str
    evaluate: PolicyFn
    is_hard: bool = False   # hard=True → multiplier 0 eliminates the worker


def _min_trust_policy(min_trust: float) -> PolicyRule:
    def _eval(p: CapabilityProfile, r: TaskRequirements) -> float:
        return 0.0 if p.trust_score < min_trust else 1.0
    return PolicyRule(name=f"min_trust_{min_trust}", evaluate=_eval, is_hard=True)


def _region_affinity_boost(preferred_region: str, boost: float = 1.2) -> PolicyRule:
    def _eval(p: CapabilityProfile, r: TaskRequirements) -> float:
        return boost if p.region == preferred_region else 1.0
    return PolicyRule(name=f"region_boost_{preferred_region}", evaluate=_eval)


_DEFAULT_RULES: list[PolicyRule] = [
    _min_trust_policy(0.3),   # eliminate workers with trust < 0.3
]


class PolicyRanker:
    """
    Re-ranks a list of CapabilityScores after applying policy rules.
    Hard policies set total_score = 0 and mark explanation with [policy blocked].
    """

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self._rules = rules if rules is not None else _DEFAULT_RULES

    def apply(
        self,
        scores: list[CapabilityScore],
        profiles: dict[str, CapabilityProfile],
        requirements: TaskRequirements,
    ) -> list[CapabilityScore]:
        result: list[CapabilityScore] = []
        for score in scores:
            profile = profiles.get(score.worker_id)
            if profile is None:
                result.append(score)
                continue
            multiplier = 1.0
            blocked = False
            for rule in self._rules:
                m = rule.evaluate(profile, requirements)
                if rule.is_hard and m == 0.0:
                    blocked = True
                    break
                multiplier *= m

            if blocked:
                score.total_score = 0.0
                score.explanation = "[policy blocked] " + score.explanation
                logger.debug("PolicyRanker: blocked %s by policy", score.worker_id)
            else:
                score.total_score = round(min(score.total_score * multiplier, 1.0), 4)
            result.append(score)

        return sorted(result, key=lambda s: s.total_score, reverse=True)
