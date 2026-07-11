"""
End-to-end integration tests — full distributed pipeline.

Tests wire together: transport → publisher → consumer → worker → scheduler.
All in-memory. No Kafka, no Redis.

CT-T4-001: Full publish → receive → execute pipeline
CT-T4-002: Fan-out event delivery to multiple workers
CT-T4-003: Lease race condition — only one winner
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.execution.states import ExecutionState
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.scheduler.contracts import WorkerView
from app.distributed.scheduler.scheduler import DistributedScheduler
from app.distributed.scheduler.strategies import LeastLoadedStrategy
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.worker.runtime import WorkerRuntime


def _id(n: str) -> NodeIdentity:
    return NodeIdentity(node_id=n, host="127.0.0.1", port=9000)


def _view(node_id: str) -> WorkerView:
    return WorkerView(node_id=node_id, is_healthy=True, in_flight_tasks=0)


class TestFullPipeline:
    """CT-T4-001: publish → route → deliver → execute → complete."""

    @pytest.mark.asyncio
    async def test_task_executes_end_to_end(self):
        transport = InMemoryTransport()
        await transport.start()
        ser = JsonEventSerializer()
        clock = MonotonicClock()
        router = DefaultEventRouter()

        publisher = DefaultEventPublisher(
            clock=clock, router=router, serializer=ser,
            transport=transport, source_node_id="sched",
        )
        consumer = DefaultEventConsumer(transport, ser, node_id="w1")
        lease_mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        cp_engine = CheckpointEngine(InMemoryCheckpointStore())

        worker = WorkerRuntime(
            identity=_id("w1"),
            publisher=publisher,
            consumer=consumer,
            lease_manager=lease_mgr,
            checkpoint_engine=cp_engine,
            max_in_flight=4,
            heartbeat_interval=9999,
        )

        executed = []

        async def handler(ctx, cb):
            executed.append(ctx.task_id)
            return {"ok": True}

        worker.register_handler("ping", handler)
        await worker.start()

        ctx = ExecutionContext(
            task_id="t-001", workflow_id="wf-1", step_id="s1",
            task_type="ping", priority="normal",
        )
        ctx.transition(ExecutionState.QUEUED)

        scheduler = DistributedScheduler(
            strategy=LeastLoadedStrategy(),
            lease_manager=lease_mgr,
            publisher=publisher,
        )
        decision = await scheduler.schedule(ctx, [_view("w1")])

        # Deliver the event to the worker
        envelope = EventEnvelope(
            event_type=DistributedEventType.TASK_ACCEPTED,
            payload={
                "task_id": ctx.task_id,
                "workflow_id": ctx.workflow_id,
                "step_id": ctx.step_id,
                "task_type": ctx.task_type,
                "task_payload": {},
                "priority": ctx.priority,
                "lease_key": ctx.lease_key,
                "fencing_token": ctx.fencing_token,
                "assigned_worker_id": "w1",
                "attempt": 0,
                "max_attempts": 3,
            },
            source_node_id="sched",
        )
        await worker._on_task_accepted(envelope)
        await asyncio.sleep(0.1)

        assert "t-001" in executed
        await worker.stop()
        await transport.stop()


class TestFanOut:
    """CT-T4-002: cluster events delivered to all workers (fan-out)."""

    @pytest.mark.asyncio
    async def test_cluster_event_reaches_all_workers(self):
        transport = InMemoryTransport()
        await transport.start()
        ser = JsonEventSerializer()
        clock = MonotonicClock()
        router = DefaultEventRouter()
        publisher = DefaultEventPublisher(
            clock=clock, router=router, serializer=ser, transport=transport,
        )

        received_by: list[str] = []

        async def make_sub(node_id: str):
            consumer = DefaultEventConsumer(transport, ser, node_id=node_id)

            async def handler(envelope: EventEnvelope):
                received_by.append(node_id)

            await consumer.subscribe([DistributedEventType.NODE_JOINED], handler, node_id)
            await consumer.start()
            return consumer

        c1 = await make_sub("w1")
        c2 = await make_sub("w2")
        c3 = await make_sub("w3")

        # Publish a cluster event — should fan out to all 3 per-worker groups
        await publisher.publish(EventEnvelope(
            event_type=DistributedEventType.NODE_JOINED,
            payload={"node_id": "new-node"},
        ))
        await asyncio.sleep(0.05)

        assert len(received_by) == 3
        assert set(received_by) == {"w1", "w2", "w3"}

        await transport.stop()


class TestLeaseRaceCondition:
    """CT-T4-003: Only one of N concurrent acquirers gets the lease."""

    @pytest.mark.asyncio
    async def test_concurrent_lease_acquisition(self):
        lease_mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        lease_key = "exec:race:step-1"

        results = await asyncio.gather(*[
            lease_mgr.acquire(lease_key, f"worker-{i}")
            for i in range(10)
        ])
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, "Exactly one worker must acquire the lease"
