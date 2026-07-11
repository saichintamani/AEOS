"""
Unit tests — all 6 scheduling strategies and DistributedScheduler.

Contract: AC-SCHED-001
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.execution.states import ExecutionState
from app.distributed.scheduler.contracts import SchedulingError, SchedulingRequest, WorkerView
from app.distributed.scheduler.scheduler import DistributedScheduler
from app.distributed.scheduler.strategies import (
    AffinityStrategy,
    CapabilityAwareStrategy,
    LeastLoadedStrategy,
    LocalityAwareStrategy,
    PriorityAwareStrategy,
    RoundRobinStrategy,
)
from app.distributed.transport.test import TestTransport


def _view(node_id: str, load: float = 0.0, caps: frozenset[str] = frozenset(),
          region: str = "us-east-1", az: str = "a",
          in_flight: int = 0) -> WorkerView:
    return WorkerView(
        node_id=node_id,
        cpu_utilization=load,
        in_flight_tasks=in_flight,
        capabilities=caps,
        region=region,
        az=az,
    )


_REQ = SchedulingRequest(task_id="t1", workflow_id="wf", step_id="s1")


class TestRoundRobinStrategy:

    def test_cycles_through_workers(self):
        strat = RoundRobinStrategy()
        workers = [_view("w1"), _view("w2"), _view("w3")]
        selected = [strat.select(workers, _REQ).worker.node_id for _ in range(6)]
        assert selected == ["w1", "w2", "w3", "w1", "w2", "w3"]

    def test_no_healthy_returns_none(self):
        strat = RoundRobinStrategy()
        unhealthy = [WorkerView(node_id="w1", is_healthy=False, in_flight_tasks=99, max_in_flight=1)]
        assert strat.select(unhealthy, _REQ) is None


class TestLeastLoadedStrategy:

    def test_picks_least_loaded(self):
        strat = LeastLoadedStrategy()
        workers = [_view("w1", load=0.8), _view("w2", load=0.1), _view("w3", load=0.5)]
        d = strat.select(workers, _REQ)
        assert d.worker.node_id == "w2"

    def test_no_workers_returns_none(self):
        assert LeastLoadedStrategy().select([], _REQ) is None


class TestCapabilityAwareStrategy:

    def test_picks_capable_worker(self):
        strat = CapabilityAwareStrategy()
        workers = [
            _view("w1", caps=frozenset({"gpu"})),
            _view("w2", caps=frozenset({"cpu"})),
        ]
        req = SchedulingRequest(required_capabilities=frozenset({"gpu"}))
        d = strat.select(workers, req)
        assert d.worker.node_id == "w1"

    def test_no_capable_worker_returns_none(self):
        strat = CapabilityAwareStrategy()
        workers = [_view("w1", caps=frozenset({"cpu"}))]
        req = SchedulingRequest(required_capabilities=frozenset({"gpu"}))
        assert strat.select(workers, req) is None


class TestPriorityAwareStrategy:

    def test_critical_task_goes_to_least_loaded(self):
        strat = PriorityAwareStrategy()
        workers = [_view("w1", load=0.9), _view("w2", load=0.1)]
        req = SchedulingRequest(priority="critical")
        d = strat.select(workers, req)
        assert d.worker.node_id == "w2"


class TestLocalityAwareStrategy:

    def test_same_az_preferred(self):
        strat = LocalityAwareStrategy()
        workers = [
            _view("w1", region="eu-west-1", az="a"),
            _view("w2", region="us-east-1", az="b"),
        ]
        req = SchedulingRequest(preferred_az="b", preferred_region="us-east-1")
        d = strat.select(workers, req)
        assert d.worker.node_id == "w2"


class TestAffinityStrategy:

    def test_pins_to_affinity_node(self):
        strat = AffinityStrategy()
        workers = [_view("w1"), _view("w2"), _view("w3")]
        req = SchedulingRequest(affinity_node_id="w2")
        d = strat.select(workers, req)
        assert d.worker.node_id == "w2"
        assert d.metadata.get("pinned") is True

    def test_anti_affinity_excludes_node(self):
        strat = AffinityStrategy()
        workers = [_view("w1"), _view("w2")]
        req = SchedulingRequest(anti_affinity_node_id="w1")
        d = strat.select(workers, req)
        assert d.worker.node_id == "w2"

    def test_affinity_node_unavailable_falls_back(self):
        strat = AffinityStrategy()
        workers = [_view("w1"), _view("w2")]
        req = SchedulingRequest(affinity_node_id="w-gone")
        d = strat.select(workers, req)
        assert d is not None  # fallback selects some healthy worker


class TestDistributedScheduler:

    def _make_scheduler(self) -> tuple[DistributedScheduler, TestTransport]:
        transport = TestTransport()
        publisher = DefaultEventPublisher(
            clock=MonotonicClock(),
            router=DefaultEventRouter(),
            serializer=JsonEventSerializer(),
            transport=transport,
        )
        lease_mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        scheduler = DistributedScheduler(
            strategy=LeastLoadedStrategy(),
            lease_manager=lease_mgr,
            publisher=publisher,
        )
        return scheduler, transport

    @pytest.mark.asyncio
    async def test_schedule_transitions_to_leased(self):
        scheduler, _ = self._make_scheduler()
        ctx = ExecutionContext(
            task_id="t1", workflow_id="wf", step_id="s1", task_type="echo",
        )
        ctx.transition(ExecutionState.QUEUED)
        workers = [_view("w1")]
        decision = await scheduler.schedule(ctx, workers)
        assert ctx.state == ExecutionState.LEASED
        assert ctx.assigned_worker_id == "w1"
        assert decision.worker.node_id == "w1"

    @pytest.mark.asyncio
    async def test_no_workers_raises(self):
        scheduler, _ = self._make_scheduler()
        ctx = ExecutionContext(
            task_id="t1", workflow_id="wf", step_id="s1",
        )
        ctx.transition(ExecutionState.QUEUED)
        with pytest.raises(SchedulingError):
            await scheduler.schedule(ctx, [])

    @pytest.mark.asyncio
    async def test_scheduled_count_increments(self):
        scheduler, _ = self._make_scheduler()
        ctx = ExecutionContext(task_id="t1", workflow_id="wf", step_id="s1")
        ctx.transition(ExecutionState.QUEUED)
        await scheduler.schedule(ctx, [_view("w1")])
        assert scheduler.scheduled_count == 1
