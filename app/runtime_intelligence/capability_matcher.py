"""
Wave 9B.3.1 — Capability Matcher, Scorer, Ranker, Resolver

Instead of "Worker 4", AEOS says:
  "Best worker is Worker 7 because: GPU available, 95% success,
   lowest latency, policy compliant, lowest cost."

CapabilityMatcher  — scores a single (profile, requirements) pair
CapabilityRanker   — ranks all profiles for a requirement
CapabilityResolver — resolves the best match, with fallback logic
"""

from __future__ import annotations

import logging
import math

from app.runtime_intelligence.contracts import (
    CapabilityMatcher,
    CapabilityProfile,
    CapabilityScore,
    TaskRequirements,
)

logger = logging.getLogger(__name__)


class DefaultCapabilityMatcher(CapabilityMatcher):
    """
    Multi-dimensional scorer.

    Dimensions and weights:
      capability_score  0.25  — skills/models/GPU/memory hard match
      trust_score       0.20  — governance trust
      load_score        0.20  — 1 - current_load penalty
      latency_score     0.15  — how well latency fits the constraint
      cost_score        0.10  — how well cost fits the constraint
      locality_score    0.10  — region/AZ affinity
    """

    _WEIGHTS = {
        "capability": 0.25,
        "trust":      0.20,
        "load":       0.20,
        "latency":    0.15,
        "cost":       0.10,
        "locality":   0.10,
    }

    def score(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
    ) -> CapabilityScore:
        cap   = self._capability_score(profile, requirements)
        trust = profile.trust_score
        load  = 1.0 - min(profile.current_load, 1.0)
        lat   = self._latency_score(profile, requirements)
        cost  = self._cost_score(profile, requirements)
        loc   = self._locality_score(profile, requirements)

        total = (
            cap   * self._WEIGHTS["capability"]
            + trust * self._WEIGHTS["trust"]
            + load  * self._WEIGHTS["load"]
            + lat   * self._WEIGHTS["latency"]
            + cost  * self._WEIGHTS["cost"]
            + loc   * self._WEIGHTS["locality"]
        )

        explanation = self._explain(profile, requirements, cap, trust, load, lat, cost, loc, total)

        return CapabilityScore(
            worker_id=profile.worker_id,
            task_id=requirements.task_id,
            total_score=round(total, 4),
            resource_score=cap,
            latency_score=lat,
            cost_score=cost,
            trust_score=trust,
            capability_score=cap,
            load_score=load,
            locality_score=loc,
            explanation=explanation,
        )

    def rank(
        self,
        profiles: list[CapabilityProfile],
        requirements: TaskRequirements,
    ) -> list[CapabilityScore]:
        scores = [self.score(p, requirements) for p in profiles if p.is_healthy]
        eligible = [s for s in scores if s.is_eligible]
        return sorted(eligible, key=lambda s: s.total_score, reverse=True)

    # ── Dimension scorers ─────────────────────────────────────────────────────

    def _capability_score(
        self,
        p: CapabilityProfile,
        r: TaskRequirements,
    ) -> float:
        """Hard constraints: GPU, memory, skills, models. Returns 0 on hard fail."""
        if r.requires_gpu and not p.gpu_available:
            return 0.0
        if r.requires_gpu and r.required_gpu_memory_gb > 0 and p.gpu_memory_gb < r.required_gpu_memory_gb:
            return 0.0
        if p.memory_gb < r.required_memory_gb:
            return 0.0

        # Soft: skills
        if r.required_skills:
            matched = len(r.required_skills & p.skills)
            skill_ratio = matched / len(r.required_skills)
            if skill_ratio == 0.0:
                return 0.0
        else:
            skill_ratio = 1.0

        # Soft: models
        if r.required_models:
            model_set = set(p.supported_models)
            matched_models = sum(1 for m in r.required_models if m in model_set)
            model_ratio = matched_models / len(r.required_models)
            if model_ratio == 0.0:
                return 0.0
        else:
            model_ratio = 1.0

        # Blend skill and model ratios with success rate
        base = 0.4 * skill_ratio + 0.4 * model_ratio + 0.2 * p.historical_success_rate
        return min(base, 1.0)

    def _latency_score(self, p: CapabilityProfile, r: TaskRequirements) -> float:
        if r.max_latency_ms == float("inf") or r.max_latency_ms <= 0:
            return 1.0
        if p.avg_latency_ms <= 0:
            return 0.5  # unknown latency → neutral
        if p.avg_latency_ms > r.max_latency_ms:
            return 0.0
        # Linear score: lower latency → higher score
        return 1.0 - (p.avg_latency_ms / r.max_latency_ms)

    def _cost_score(self, p: CapabilityProfile, r: TaskRequirements) -> float:
        if r.max_cost == float("inf") or r.max_cost <= 0:
            return 1.0
        if p.token_cost_per_k <= 0:
            return 0.5
        if p.token_cost_per_k > r.max_cost:
            return 0.0
        return 1.0 - (p.token_cost_per_k / r.max_cost)

    def _locality_score(self, p: CapabilityProfile, r: TaskRequirements) -> float:
        if r.preferred_az and p.az == r.preferred_az:
            return 1.0
        if r.preferred_region and p.region == r.preferred_region:
            return 0.6
        return 0.2  # non-zero to allow cross-region fallback

    def _explain(
        self,
        p: CapabilityProfile,
        r: TaskRequirements,
        cap: float, trust: float, load: float,
        lat: float, cost: float, loc: float,
        total: float,
    ) -> str:
        parts = [f"Worker {p.worker_id} selected — total_score={total:.3f}"]
        if p.gpu_available:
            parts.append("GPU available")
        parts.append(f"success_rate={p.historical_success_rate:.0%}")
        parts.append(f"trust={p.trust_score:.2f}")
        parts.append(f"load={p.current_load:.0%}")
        if lat > 0.8:
            parts.append("low latency")
        if cost > 0.8:
            parts.append("cost within budget")
        if loc >= 0.6:
            parts.append(f"locality={p.region}/{p.az}")
        return " | ".join(parts)


class CapabilityRanker:
    """
    Ranks workers for a task, applying optional affinity/anti-affinity constraints.
    """

    def __init__(self, matcher: CapabilityMatcher | None = None) -> None:
        self._matcher = matcher or DefaultCapabilityMatcher()

    def rank(
        self,
        profiles: list[CapabilityProfile],
        requirements: TaskRequirements,
    ) -> list[CapabilityScore]:
        # Hard affinity pin
        if requirements.affinity_worker_id:
            pinned = [p for p in profiles if p.worker_id == requirements.affinity_worker_id]
            if pinned:
                return self._matcher.rank(pinned, requirements)

        # Hard anti-affinity exclusion
        if requirements.anti_affinity_worker_id:
            profiles = [p for p in profiles if p.worker_id != requirements.anti_affinity_worker_id]

        return self._matcher.rank(profiles, requirements)


class CapabilityResolver:
    """
    Resolves the single best worker for a task.

    Falls back through tiers:
      1. Fully eligible workers (all hard constraints satisfied)
      2. Healthy workers ignoring latency/cost constraints
      3. Any healthy worker (last resort)
    """

    def __init__(self, matcher: CapabilityMatcher | None = None) -> None:
        self._ranker = CapabilityRanker(matcher)

    def resolve(
        self,
        profiles: list[CapabilityProfile],
        requirements: TaskRequirements,
    ) -> CapabilityScore | None:
        ranked = self._ranker.rank(profiles, requirements)
        if ranked:
            best = ranked[0]
            logger.info(
                "CapabilityResolver: selected %s (score=%.3f) — %s",
                best.worker_id, best.total_score, best.explanation,
            )
            return best

        # Fallback: any healthy worker
        healthy = [p for p in profiles if p.is_healthy]
        if not healthy:
            return None

        relaxed_req = TaskRequirements(
            task_type=requirements.task_type,
            task_id=requirements.task_id,
            required_skills=frozenset(),
            max_latency_ms=float("inf"),
            max_cost=float("inf"),
        )
        ranked_fallback = self._ranker.rank(healthy, relaxed_req)
        if ranked_fallback:
            best = ranked_fallback[0]
            best.explanation = "[fallback] " + best.explanation
            logger.warning(
                "CapabilityResolver: fallback to %s — relaxed constraints",
                best.worker_id,
            )
            return best

        return None
