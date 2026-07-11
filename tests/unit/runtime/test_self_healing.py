"""Unit tests — SelfHealingRuntime."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import CapabilityProfile
from app.runtime.self_healing import (
    FailureContext,
    RecoveryStrategy,
    SelfHealingRuntime,
)


def _profile(worker_id: str, health_score: float = 1.0) -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        health_score=health_score,
        trust_score=0.9,
        historical_success_rate=0.95,
    )


def _failure(task_id="t1", worker_id="w1", attempt=0, max_attempts=3,
             has_checkpoint=False, committed=False) -> FailureContext:
    return FailureContext(
        task_id=task_id,
        worker_id=worker_id,
        task_type="test",
        attempt=attempt,
        max_attempts=max_attempts,
        has_checkpoint=has_checkpoint,
        checkpoint_committed=committed,
    )


class TestSelfHealingRuntime:

    @pytest.mark.asyncio
    async def test_escalate_when_max_attempts_exceeded(self):
        shr = SelfHealingRuntime()
        workers = [_profile("w1"), _profile("w2")]
        action = await shr.heal(_failure(attempt=3, max_attempts=3), workers)
        assert action.strategy == RecoveryStrategy.ESCALATE

    @pytest.mark.asyncio
    async def test_resume_with_committed_checkpoint(self):
        shr = SelfHealingRuntime()
        workers = [_profile("w1"), _profile("w2")]
        f = _failure(has_checkpoint=True, committed=True)
        action = await shr.heal(f, workers)
        assert action.strategy == RecoveryStrategy.RESUME
        assert action.resume_from_checkpoint

    @pytest.mark.asyncio
    async def test_migrate_when_no_checkpoint_and_other_worker(self):
        shr = SelfHealingRuntime()
        workers = [_profile("w1"), _profile("w2")]
        f = _failure(worker_id="w1", has_checkpoint=False)
        action = await shr.heal(f, workers)
        assert action.strategy == RecoveryStrategy.MIGRATE
        assert action.target_worker_id == "w2"

    @pytest.mark.asyncio
    async def test_requeue_when_no_healthy_alternatives(self):
        shr = SelfHealingRuntime()
        workers = [_profile("w1")]  # only the failed worker
        f = _failure(worker_id="w1")
        action = await shr.heal(f, workers)
        assert action.strategy == RecoveryStrategy.REQUEUE

    @pytest.mark.asyncio
    async def test_heal_updates_learning_engine(self):
        from app.runtime_intelligence.learning_engine import DefaultLearningEngine
        learning = DefaultLearningEngine()
        shr = SelfHealingRuntime(learning_engine=learning)
        workers = [_profile("w1"), _profile("w2")]
        await shr.heal(_failure(worker_id="w1"), workers)
        # Record should be in the learning engine
        stats = await learning.worker_stats("w1")
        assert len(stats) > 0

    @pytest.mark.asyncio
    async def test_unhealthy_workers_excluded(self):
        shr = SelfHealingRuntime()
        workers = [_profile("w1"), _profile("w2", health_score=0.1)]
        f = _failure(worker_id="w1")
        action = await shr.heal(f, workers)
        # w2 is unhealthy so no migration target
        assert action.strategy == RecoveryStrategy.REQUEUE
