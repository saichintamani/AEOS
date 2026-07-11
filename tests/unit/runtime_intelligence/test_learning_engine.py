"""Unit tests — DefaultLearningEngine."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import ExecutionRecord
from app.runtime_intelligence.learning_engine import DefaultLearningEngine


def _rec(worker_id="w1", task_type="nlp", success=True,
         latency_ms=100.0, cost=0.01, model="gpt-4") -> ExecutionRecord:
    return ExecutionRecord(
        worker_id=worker_id,
        task_type=task_type,
        success=success,
        latency_ms=latency_ms,
        cost=cost,
        model_used=model,
    )


class TestDefaultLearningEngine:

    @pytest.mark.asyncio
    async def test_predict_success_rate_optimistic_prior(self):
        engine = DefaultLearningEngine()
        rate = await engine.predict_success_rate("unknown-worker", "unknown-task")
        assert rate == 1.0

    @pytest.mark.asyncio
    async def test_record_updates_stats(self):
        engine = DefaultLearningEngine()
        for _ in range(5):
            await engine.record(_rec(success=True))
        rate = await engine.predict_success_rate("w1", "nlp")
        assert rate > 0.9

    @pytest.mark.asyncio
    async def test_failures_lower_success_rate(self):
        engine = DefaultLearningEngine()
        for _ in range(10):
            await engine.record(_rec(success=False))
        rate = await engine.predict_success_rate("w1", "nlp")
        assert rate < 0.5

    @pytest.mark.asyncio
    async def test_recommend_model_returns_best(self):
        engine = DefaultLearningEngine()
        # gpt-4: 5 successes
        for _ in range(5):
            await engine.record(_rec(model="gpt-4", success=True, latency_ms=200))
        # llama-3: 5 failures
        for _ in range(5):
            await engine.record(_rec(model="llama-3", success=False, latency_ms=100))
        model = await engine.recommend_model("nlp")
        assert model == "gpt-4"

    @pytest.mark.asyncio
    async def test_recommend_model_none_when_no_data(self):
        engine = DefaultLearningEngine()
        model = await engine.recommend_model("unknown-task")
        assert model is None

    @pytest.mark.asyncio
    async def test_recommend_model_requires_min_records(self):
        engine = DefaultLearningEngine()
        # Only 2 records — below min of 3
        await engine.record(_rec(model="gpt-4", success=True))
        await engine.record(_rec(model="gpt-4", success=True))
        model = await engine.recommend_model("nlp")
        assert model is None

    @pytest.mark.asyncio
    async def test_worker_stats(self):
        engine = DefaultLearningEngine()
        await engine.record(_rec(worker_id="w1", task_type="nlp"))
        await engine.record(_rec(worker_id="w1", task_type="vision"))
        stats = await engine.worker_stats("w1")
        task_types = {s.task_type for s in stats}
        assert "nlp" in task_types
        assert "vision" in task_types

    @pytest.mark.asyncio
    async def test_ema_converges(self):
        engine = DefaultLearningEngine()
        # Mix of successes and failures
        for i in range(20):
            await engine.record(_rec(success=(i % 2 == 0)))
        rate = await engine.predict_success_rate("w1", "nlp")
        # Should be somewhere around 0.5 after mixing
        assert 0.3 <= rate <= 0.7
