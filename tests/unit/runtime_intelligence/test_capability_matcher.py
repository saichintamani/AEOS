"""Unit tests — DefaultCapabilityMatcher, CapabilityRanker, CapabilityResolver."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.capability_matcher import (
    CapabilityRanker,
    CapabilityResolver,
    DefaultCapabilityMatcher,
)
from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements


def _profile(
    worker_id: str = "w1",
    memory_gb: float = 8.0,
    gpu: bool = False,
    gpu_memory_gb: float = 0.0,
    trust: float = 0.9,
    load: float = 0.1,
    latency_ms: float = 100.0,
    cost: float = 0.01,
    skills: frozenset[str] = frozenset(),
    models: list[str] | None = None,
    region: str = "us-east-1",
    az: str = "a",
    health_score: float = 1.0,
    success_rate: float = 0.95,
) -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        memory_gb=memory_gb,
        gpu_available=gpu,
        gpu_memory_gb=gpu_memory_gb,
        trust_score=trust,
        current_load=load,
        avg_latency_ms=latency_ms,
        token_cost_per_k=cost,
        skills=skills,
        supported_models=models or [],
        region=region,
        az=az,
        health_score=health_score,
        historical_success_rate=success_rate,
    )


def _req(**kwargs) -> TaskRequirements:
    return TaskRequirements(**kwargs)


class TestDefaultCapabilityMatcherHardConstraints:

    def test_gpu_hard_fail(self):
        m = DefaultCapabilityMatcher()
        p = _profile(gpu=False)
        r = _req(requires_gpu=True)
        score = m.score(p, r)
        assert score.capability_score == 0.0
        assert not score.is_eligible

    def test_gpu_memory_hard_fail(self):
        m = DefaultCapabilityMatcher()
        p = _profile(gpu=True, gpu_memory_gb=4.0)
        r = _req(requires_gpu=True, required_gpu_memory_gb=8.0)
        score = m.score(p, r)
        assert score.capability_score == 0.0

    def test_memory_hard_fail(self):
        m = DefaultCapabilityMatcher()
        p = _profile(memory_gb=4.0)
        r = _req(required_memory_gb=8.0)
        score = m.score(p, r)
        assert score.capability_score == 0.0

    def test_skill_full_miss(self):
        m = DefaultCapabilityMatcher()
        p = _profile(skills=frozenset({"vision"}))
        r = _req(required_skills=frozenset({"nlp", "rag"}))
        score = m.score(p, r)
        assert score.capability_score == 0.0

    def test_model_full_miss(self):
        m = DefaultCapabilityMatcher()
        p = _profile(models=["llama-3"])
        r = _req(required_models=["gpt-4"])
        score = m.score(p, r)
        assert score.capability_score == 0.0


class TestDefaultCapabilityMatcherScores:

    def test_perfect_match(self):
        m = DefaultCapabilityMatcher()
        p = _profile(skills=frozenset({"nlp"}), models=["gpt-4"], success_rate=1.0)
        r = _req(required_skills=frozenset({"nlp"}), required_models=["gpt-4"])
        score = m.score(p, r)
        assert score.capability_score > 0.8
        assert score.is_eligible

    def test_latency_within_budget(self):
        m = DefaultCapabilityMatcher()
        p = _profile(latency_ms=50.0)
        r = _req(max_latency_ms=200.0)
        score = m.score(p, r)
        assert score.latency_score > 0.5

    def test_latency_over_budget(self):
        m = DefaultCapabilityMatcher()
        p = _profile(latency_ms=300.0)
        r = _req(max_latency_ms=200.0)
        score = m.score(p, r)
        assert score.latency_score == 0.0

    def test_latency_no_constraint(self):
        m = DefaultCapabilityMatcher()
        p = _profile(latency_ms=500.0)
        r = _req()
        score = m.score(p, r)
        assert score.latency_score == 1.0

    def test_cost_within_budget(self):
        m = DefaultCapabilityMatcher()
        p = _profile(cost=0.005)
        r = _req(max_cost=0.01)
        score = m.score(p, r)
        assert score.cost_score == 0.5

    def test_cost_over_budget(self):
        m = DefaultCapabilityMatcher()
        p = _profile(cost=0.02)
        r = _req(max_cost=0.01)
        score = m.score(p, r)
        assert score.cost_score == 0.0

    def test_locality_exact_az(self):
        m = DefaultCapabilityMatcher()
        p = _profile(region="us-east-1", az="b")
        r = _req(preferred_az="b")
        score = m.score(p, r)
        assert score.locality_score == 1.0

    def test_locality_region_match(self):
        m = DefaultCapabilityMatcher()
        p = _profile(region="us-east-1", az="c")
        r = _req(preferred_region="us-east-1", preferred_az="b")
        score = m.score(p, r)
        assert score.locality_score == 0.6

    def test_locality_no_preference(self):
        m = DefaultCapabilityMatcher()
        p = _profile(region="eu-west-1")
        r = _req()
        score = m.score(p, r)
        assert score.locality_score == 0.2

    def test_total_score_in_range(self):
        m = DefaultCapabilityMatcher()
        p = _profile()
        r = _req()
        score = m.score(p, r)
        assert 0.0 <= score.total_score <= 1.0

    def test_explanation_contains_worker_id(self):
        m = DefaultCapabilityMatcher()
        p = _profile(worker_id="worker-99")
        r = _req()
        score = m.score(p, r)
        assert "worker-99" in score.explanation


class TestCapabilityRanker:

    def test_rank_orders_by_score(self):
        r = CapabilityRanker()
        profiles = [
            _profile("w1", load=0.9, trust=0.5),
            _profile("w2", load=0.1, trust=0.95),
        ]
        req = _req()
        ranked = r.rank(profiles, req)
        assert len(ranked) >= 1
        # w2 should rank higher due to lower load and higher trust
        assert ranked[0].worker_id == "w2"

    def test_rank_excludes_unhealthy(self):
        r = CapabilityRanker()
        profiles = [
            _profile("w1", health_score=1.0),
            _profile("w2", health_score=0.2),
        ]
        req = _req()
        ranked = r.rank(profiles, req)
        ids = {s.worker_id for s in ranked}
        assert "w2" not in ids

    def test_affinity_pin(self):
        r = CapabilityRanker()
        profiles = [_profile("w1"), _profile("w2"), _profile("w3")]
        req = _req(affinity_worker_id="w2")
        ranked = r.rank(profiles, req)
        assert len(ranked) == 1
        assert ranked[0].worker_id == "w2"

    def test_anti_affinity_exclusion(self):
        r = CapabilityRanker()
        profiles = [_profile("w1"), _profile("w2"), _profile("w3")]
        req = _req(anti_affinity_worker_id="w1")
        ranked = r.rank(profiles, req)
        ids = {s.worker_id for s in ranked}
        assert "w1" not in ids
        assert "w2" in ids

    def test_empty_profiles_returns_empty(self):
        r = CapabilityRanker()
        ranked = r.rank([], _req())
        assert ranked == []


class TestCapabilityResolver:

    def test_resolve_returns_best(self):
        resolver = CapabilityResolver()
        profiles = [
            _profile("w1", load=0.1, trust=0.95),
            _profile("w2", load=0.8, trust=0.5),
        ]
        result = resolver.resolve(profiles, _req())
        assert result is not None
        assert result.worker_id == "w1"

    def test_resolve_fallback_on_no_eligible(self):
        resolver = CapabilityResolver()
        # GPU required but no workers have GPU
        profiles = [_profile("w1", gpu=False), _profile("w2", gpu=False)]
        req = _req(requires_gpu=True)
        result = resolver.resolve(profiles, req)
        # Falls back to healthy workers — worker is still healthy, just can't meet GPU requirement
        # Fallback relaxes constraints, so we get a result
        assert result is not None
        assert "[fallback]" in result.explanation

    def test_resolve_none_when_no_healthy(self):
        resolver = CapabilityResolver()
        profiles = [_profile("w1", health_score=0.1), _profile("w2", health_score=0.2)]
        result = resolver.resolve(profiles, _req())
        assert result is None

    def test_resolve_empty_returns_none(self):
        resolver = CapabilityResolver()
        result = resolver.resolve([], _req())
        assert result is None
