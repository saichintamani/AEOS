"""Unit tests — ExpectedUtilityDecisionEngine, AIScheduler, ExplanationEngine."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    ExecutionRecord,
    TaskRequirements,
)
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.explanation_engine import ExplanationEngine, Verbosity
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime_intelligence.scheduler_ai import AIScheduler


def _profile(worker_id="w1", trust=0.9, load=0.1, latency_ms=100.0,
             cost=0.01, skills=None, gpu=False, memory_gb=8.0,
             health_score=1.0, success_rate=0.95, region="us-east-1") -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        trust_score=trust,
        current_load=load,
        avg_latency_ms=latency_ms,
        token_cost_per_k=cost,
        skills=frozenset(skills or []),
        gpu_available=gpu,
        memory_gb=memory_gb,
        health_score=health_score,
        historical_success_rate=success_rate,
        region=region,
    )


def _req(**kwargs) -> TaskRequirements:
    return TaskRequirements(**kwargs)


class TestExpectedUtilityDecisionEngine:

    @pytest.mark.asyncio
    async def test_selects_best_worker(self):
        engine = ExpectedUtilityDecisionEngine()
        profiles = [
            _profile("w1", trust=0.5, load=0.9),
            _profile("w2", trust=0.95, load=0.1),
        ]
        decision = await engine.decide(_req(), profiles)
        assert decision.worker_id == "w2"

    @pytest.mark.asyncio
    async def test_no_eligible_workers_returns_empty(self):
        engine = ExpectedUtilityDecisionEngine()
        decision = await engine.decide(_req(), [])
        assert decision.worker_id == ""
        assert decision.expected_utility == 0.0

    @pytest.mark.asyncio
    async def test_decision_has_dimensions(self):
        engine = ExpectedUtilityDecisionEngine()
        profiles = [_profile("w1")]
        decision = await engine.decide(_req(), profiles)
        assert len(decision.dimensions) == 6
        dim_names = {d.name for d in decision.dimensions}
        assert "capability" in dim_names
        assert "trust" in dim_names

    @pytest.mark.asyncio
    async def test_confidence_between_0_and_1(self):
        engine = ExpectedUtilityDecisionEngine()
        profiles = [_profile("w1"), _profile("w2")]
        decision = await engine.decide(_req(), profiles)
        assert 0.0 <= decision.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_alternatives_listed(self):
        engine = ExpectedUtilityDecisionEngine()
        profiles = [_profile(f"w{i}") for i in range(4)]
        decision = await engine.decide(_req(), profiles)
        assert len(decision.alternatives) >= 1

    @pytest.mark.asyncio
    async def test_learning_influences_utility(self):
        learning = DefaultLearningEngine()
        # Give w1 many failures, w2 many successes
        for _ in range(10):
            await learning.record(ExecutionRecord(worker_id="w1", task_type="t",
                                                  success=False, latency_ms=100))
        for _ in range(10):
            await learning.record(ExecutionRecord(worker_id="w2", task_type="t",
                                                  success=True, latency_ms=100))
        engine = ExpectedUtilityDecisionEngine(learning_engine=learning)
        profiles = [
            _profile("w1", trust=0.9, load=0.1),
            _profile("w2", trust=0.9, load=0.1),
        ]
        decision = await engine.decide(_req(task_type="t"), profiles)
        assert decision.worker_id == "w2"

    @pytest.mark.asyncio
    async def test_strategy_name_set(self):
        engine = ExpectedUtilityDecisionEngine()
        profiles = [_profile("w1", gpu=True)]
        decision = await engine.decide(_req(requires_gpu=True), profiles)
        assert decision.strategy_name  # non-empty


class TestAIScheduler:

    @pytest.mark.asyncio
    async def test_basic_decision(self):
        scheduler = AIScheduler()
        profiles = [_profile("w1"), _profile("w2", trust=0.6, load=0.8)]
        decision = await scheduler.decide(_req(), profiles)
        assert decision.worker_id in {"w1", "w2"}

    @pytest.mark.asyncio
    async def test_gpu_strategy_selected(self):
        scheduler = AIScheduler()
        profiles = [_profile("w1", gpu=True)]
        decision = await scheduler.decide(_req(requires_gpu=True), profiles)
        assert decision.strategy_name == "gpu_capability"


class TestExplanationEngine:

    def test_brief_decision(self):
        engine = ExpectedUtilityDecisionEngine.__new__(ExpectedUtilityDecisionEngine)
        from app.runtime_intelligence.contracts import ExecutionDecision
        decision = ExecutionDecision(
            task_id="t1", worker_id="w1", strategy_name="capability_aware",
            expected_utility=0.85, confidence=0.9,
        )
        exp = ExplanationEngine()
        text = exp.explain_decision(decision, Verbosity.BRIEF)
        assert "w1" in text
        assert "t1" in text

    def test_full_decision_has_dimensions_header(self):
        from app.runtime_intelligence.contracts import DecisionDimension, ExecutionDecision
        decision = ExecutionDecision(
            task_id="t1", worker_id="w2", strategy_name="latency_optimized",
            expected_utility=0.77, confidence=0.82,
            dimensions=[
                DecisionDimension("capability", 0.8, 0.25),
                DecisionDimension("trust", 0.9, 0.20),
            ],
        )
        exp = ExplanationEngine()
        text = exp.explain_decision(decision, Verbosity.FULL)
        assert "Score Dimensions" in text
        assert "capability" in text

    def test_no_worker_explanation(self):
        from app.runtime_intelligence.contracts import ExecutionDecision
        decision = ExecutionDecision(
            task_id="t1", worker_id="", strategy_name="", expected_utility=0.0
        )
        exp = ExplanationEngine()
        text = exp.explain_decision(decision)
        assert "No worker" in text

    def test_decision_to_dict(self):
        from app.runtime_intelligence.contracts import ExecutionDecision
        decision = ExecutionDecision(
            task_id="t1", worker_id="w1", strategy_name="s",
            expected_utility=0.5, confidence=0.6,
        )
        exp = ExplanationEngine()
        d = exp.decision_to_dict(decision)
        assert d["task_id"] == "t1"
        assert d["worker_id"] == "w1"
        assert "dimensions" in d
