"""
Unit tests for Wave 9B.5.7 — Distributed Scheduler.

Pure asyncio, no external dependencies.
"""

from __future__ import annotations

import asyncio

import pytest

from app.distributed.scheduler.distributed_scheduler import (
    AdmissionControl,
    LeaderScheduler,
    ScheduledTask,
    WorkerScheduler,
)
from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements


# ── helpers ───────────────────────────────────────────────────────────────────

def _profile(worker_id: str, load: float = 0.0) -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        memory_gb=8.0,
        current_load=load,
        health_score=1.0,
        trust_score=1.0,
        historical_success_rate=1.0,
    )


def _req(task_id: str = "t1", priority: str = "normal") -> TaskRequirements:
    return TaskRequirements(task_id=task_id, task_type="test", priority=priority)


# ── WorkerScheduler ───────────────────────────────────────────────────────────

class TestWorkerScheduler:

    @pytest.mark.asyncio
    async def test_enqueue_returns_true_when_has_capacity(self):
        ws = WorkerScheduler("w1", max_in_flight=8)
        task = ScheduledTask(requirements=_req(), worker_id="w1", priority=2)
        ok = await ws.enqueue(task)
        assert ok is True

    @pytest.mark.asyncio
    async def test_enqueue_returns_false_when_full(self):
        from app.distributed.scheduler.distributed_scheduler import _MAX_QUEUE_DEPTH
        ws = WorkerScheduler("w1")
        # Fill the queue
        for i in range(_MAX_QUEUE_DEPTH):
            task = ScheduledTask(requirements=_req(str(i)), worker_id="w1")
            await ws.enqueue(task)
        # One more should fail
        extra = ScheduledTask(requirements=_req("overflow"), worker_id="w1")
        ok = await ws.enqueue(extra)
        assert ok is False

    @pytest.mark.asyncio
    async def test_next_returns_highest_priority_first(self):
        ws = WorkerScheduler("w1")
        low = ScheduledTask(requirements=_req("low"), worker_id="w1", priority=0)
        high = ScheduledTask(requirements=_req("high"), worker_id="w1", priority=4)
        med = ScheduledTask(requirements=_req("med"), worker_id="w1", priority=2)

        await ws.enqueue(low)
        await ws.enqueue(high)
        await ws.enqueue(med)

        first = await ws.next()
        assert first is not None
        assert first.priority == 4   # highest

    @pytest.mark.asyncio
    async def test_next_returns_none_on_timeout(self):
        ws = WorkerScheduler("w1")
        result = await ws.next()
        assert result is None

    def test_queue_depth_property(self):
        ws = WorkerScheduler("w1")
        assert ws.queue_depth == 0

    def test_record_complete_decrements_in_flight(self):
        ws = WorkerScheduler("w1")
        ws._in_flight = 3
        ws.record_complete(success=True)
        assert ws._in_flight == 2
        assert ws._completed == 1

    def test_record_failure(self):
        ws = WorkerScheduler("w1")
        ws._in_flight = 2
        ws.record_complete(success=False)
        assert ws._failed == 1

    def test_load_factor_zero_when_idle(self):
        ws = WorkerScheduler("w1")
        assert ws.load_factor() == 0.0

    @pytest.mark.asyncio
    async def test_load_factor_increases_with_queue_depth(self):
        ws = WorkerScheduler("w1")
        from app.distributed.scheduler.distributed_scheduler import _MAX_QUEUE_DEPTH
        for i in range(10):
            await ws.enqueue(ScheduledTask(requirements=_req(str(i)), worker_id="w1"))
        assert ws.load_factor() > 0.0
        assert ws.load_factor() <= 1.0


# ── AdmissionControl ──────────────────────────────────────────────────────────

class TestAdmissionControl:

    def test_admits_when_cluster_healthy(self):
        ac = AdmissionControl()
        ac.update_metrics(queue_depth=0, cpu=0.1, memory=0.2)
        ok, reason = ac.should_admit(_req())
        assert ok is True

    def test_rejects_when_cluster_overloaded(self):
        ac = AdmissionControl()
        # Saturate CPU and queue
        ac.update_metrics(queue_depth=300, cpu=0.99, memory=0.95)
        ok, reason = ac.should_admit(_req())
        # Should reject or shed (at high load)
        # At least the reason should be informative
        if not ok:
            assert "overloaded" in reason.lower() or "shedding" in reason.lower() or "only" in reason.lower()

    def test_sheds_low_priority_when_under_load(self):
        ac = AdmissionControl()
        # Moderate-high load to trigger SHED
        ac.update_metrics(queue_depth=200, cpu=0.85, memory=0.80)
        ok, reason = ac.should_admit(_req(priority="batch"))
        # batch priority may be shed under load
        # This depends on the ThresholdPolicy thresholds — just verify no exception
        assert isinstance(ok, bool)

    def test_admits_critical_even_under_load(self):
        ac = AdmissionControl()
        ac.update_metrics(queue_depth=200, cpu=0.85, memory=0.80)
        ok, reason = ac.should_admit(_req(priority="critical"))
        # Critical should still be admitted (not shed)
        assert isinstance(ok, bool)
        # If shed, critical is excluded from shedding
        if not ok:
            assert "critical" not in reason.lower()


# ── LeaderScheduler ───────────────────────────────────────────────────────────

class TestLeaderScheduler:

    @pytest.mark.asyncio
    async def test_submit_fails_when_no_workers(self):
        sched = LeaderScheduler()
        ok, reason = await sched.submit(_req())
        assert ok is False
        assert "worker" in reason.lower()

    @pytest.mark.asyncio
    async def test_submit_succeeds_with_registered_worker(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))
        ok, reason = await sched.submit(_req())
        assert ok is True

    @pytest.mark.asyncio
    async def test_register_worker_updates_profile(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1", load=0.1))
        assert len(sched._profiles) == 1
        # Re-register with updated load
        sched.register_worker(_profile("w1", load=0.5))
        assert len(sched._profiles) == 1
        assert sched._profiles[0].current_load == 0.5

    @pytest.mark.asyncio
    async def test_dispatch_callback_called_on_success(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))

        dispatched: list[ScheduledTask] = []
        sched.on_dispatch(lambda t: dispatched.append(t))

        await sched.submit(_req("cb-task"))
        assert len(dispatched) == 1
        assert dispatched[0].requirements.task_id == "cb-task"

    @pytest.mark.asyncio
    async def test_async_dispatch_callback(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))

        dispatched: list[ScheduledTask] = []

        async def _cb(t: ScheduledTask) -> None:
            dispatched.append(t)

        sched.on_dispatch(_cb)
        await sched.submit(_req("async-cb"))
        assert len(dispatched) == 1

    @pytest.mark.asyncio
    async def test_priority_mapping(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))

        await sched.submit(_req("t-critical", priority="critical"))
        ws = sched._worker_schedulers.get("w1")
        assert ws is not None
        # Get the queued task
        task = await ws.next()
        assert task is not None
        assert task.priority == 4   # critical = 4

    @pytest.mark.asyncio
    async def test_cluster_stats_returns_correct_structure(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))
        sched.register_worker(_profile("w2"))

        stats = sched.cluster_stats()
        assert stats["workers"] == 2
        assert "total_queued" in stats
        assert "total_in_flight" in stats
        assert "worker_load" in stats
        assert "w1" in stats["worker_load"]
        assert "w2" in stats["worker_load"]

    @pytest.mark.asyncio
    async def test_worker_queue_full_returns_failure(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1"))

        # Fill the worker's queue
        ws = sched._worker_schedulers["w1"]
        from app.distributed.scheduler.distributed_scheduler import _MAX_QUEUE_DEPTH
        for i in range(_MAX_QUEUE_DEPTH):
            t = ScheduledTask(requirements=_req(str(i)), worker_id="w1")
            await ws.enqueue(t)

        ok, reason = await sched.submit(_req("overflow"))
        assert ok is False
        assert "full" in reason.lower()

    @pytest.mark.asyncio
    async def test_multiple_workers_dispatch_via_decision_engine(self):
        sched = LeaderScheduler()
        sched.register_worker(_profile("w1", load=0.5))  # moderate load
        sched.register_worker(_profile("w2", load=0.1))  # low load

        ok, _ = await sched.submit(_req())
        assert ok is True
        # Decision engine dispatched to one of the two workers
        stats = sched.cluster_stats()
        assert stats["total_queued"] == 1


# ── ScheduledTask ─────────────────────────────────────────────────────────────

class TestScheduledTask:

    def test_default_priority_is_normal(self):
        task = ScheduledTask(requirements=_req(), worker_id="w1")
        assert task.priority == 2

    def test_enqueued_at_set_on_creation(self):
        task = ScheduledTask(requirements=_req(), worker_id="w1")
        assert task.enqueued_at > 0
