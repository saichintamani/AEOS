"""
Unit tests — WorkerRuntime, HeartbeatService, GovernanceClient.

Architecture Contract IDs: AC-EXEC-001, AC-EXEC-003, AC-LIFE-002
Protocol IDs: PROTO-003, PROTO-015
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.transport.test import TestTransport
from app.distributed.worker.governance import GovernanceClient, TokenRevokedException
from app.distributed.worker.heartbeat import HeartbeatService
from app.distributed.worker.runtime import WorkerRuntime


def _identity(node_id="worker-1") -> NodeIdentity:
    return NodeIdentity(node_id=node_id, host="127.0.0.1", port=9000)


def _make_worker_stack(node_id="worker-1"):
    transport = InMemoryTransport()
    ser = JsonEventSerializer()
    router = DefaultEventRouter()
    clock = MonotonicClock()
    publisher = DefaultEventPublisher(
        clock=clock, router=router, serializer=ser, transport=transport, source_node_id=node_id
    )
    consumer = DefaultEventConsumer(transport, ser, node_id=node_id)
    lease_mgr = ExecutionLeaseManager(InMemoryLeaseStore())
    cp_engine = CheckpointEngine(InMemoryCheckpointStore())
    worker = WorkerRuntime(
        identity=_identity(node_id),
        publisher=publisher,
        consumer=consumer,
        lease_manager=lease_mgr,
        checkpoint_engine=cp_engine,
        max_in_flight=4,
        heartbeat_interval=9999,
    )
    return worker, transport, publisher


# ── HeartbeatService ──────────────────────────────────────────────────────────

class TestHeartbeatService:

    @pytest.mark.asyncio
    async def test_beat_emits_event(self):
        transport = TestTransport()
        await transport.start()
        ser = JsonEventSerializer()
        router = DefaultEventRouter()
        clock = MonotonicClock()
        publisher = DefaultEventPublisher(
            clock=clock, router=router, serializer=ser, transport=transport
        )

        svc = HeartbeatService(
            publisher=publisher,
            node_id="node-1",
            metrics_provider=lambda: {"in_flight_tasks": 2, "cpu_utilization": 0.5},
            interval_seconds=9999,
        )
        await svc.beat()
        assert svc.beat_count == 1
        # Heartbeat reuses NODE_JOINED envelope → goes to aeos.events.cluster
        assert transport.published_count("aeos.events.cluster") == 1

    @pytest.mark.asyncio
    async def test_start_stop(self):
        transport = TestTransport()
        await transport.start()
        ser = JsonEventSerializer()
        publisher = DefaultEventPublisher(
            clock=MonotonicClock(),
            router=DefaultEventRouter(),
            serializer=ser,
            transport=transport,
        )
        svc = HeartbeatService(
            publisher=publisher,
            node_id="node-1",
            metrics_provider=lambda: {},
            interval_seconds=9999,
        )
        await svc.start()
        await svc.stop()   # should not raise


# ── GovernanceClient ──────────────────────────────────────────────────────────

class TestGovernanceClient:

    def _make_client(self, node_id="worker-1"):
        transport = InMemoryTransport()
        ser = JsonEventSerializer()
        consumer = DefaultEventConsumer(transport, ser, node_id=node_id)
        client = GovernanceClient(consumer, node_id)
        return client, consumer, transport

    @pytest.mark.asyncio
    async def test_token_not_revoked_by_default(self):
        client, _, _ = self._make_client()
        await client.verify_token("some-token")  # should not raise

    @pytest.mark.asyncio
    async def test_revoked_token_raises(self):
        client, consumer, transport = self._make_client()
        await transport.start()
        await consumer.start()
        await client.start()

        # Directly inject into revoked set (simulating received event)
        async with client._lock:
            client._revoked_tokens.add("bad-token")

        with pytest.raises(TokenRevokedException):
            await client.verify_token("bad-token")

        await client.stop()

    @pytest.mark.asyncio
    async def test_none_token_always_passes(self):
        client, _, _ = self._make_client()
        await client.verify_token(None)  # should not raise

    @pytest.mark.asyncio
    async def test_rbac_revocation(self):
        client, _, _ = self._make_client()
        async with client._lock:
            client._rbac_revocations.add(("alice", "resource-x"))
        allowed = await client.verify_rbac("alice", "resource-x")
        assert allowed is False

    @pytest.mark.asyncio
    async def test_rbac_allowed_by_default(self):
        client, _, _ = self._make_client()
        allowed = await client.verify_rbac("alice", "resource-y")
        assert allowed is True


# ── WorkerRuntime ─────────────────────────────────────────────────────────────

class TestWorkerRuntime:

    @pytest.mark.asyncio
    async def test_start_stop(self):
        worker, transport, _ = _make_worker_stack()
        await transport.start()
        await worker.start()
        assert worker.is_running
        await worker.stop()
        assert not worker.is_running

    @pytest.mark.asyncio
    async def test_task_executed_via_event(self):
        worker, transport, publisher = _make_worker_stack("w1")
        await transport.start()
        await worker.start()

        completed = []

        async def my_handler(ctx, callbacks):
            completed.append(ctx.task_id)
            return {"done": True}

        worker.register_handler("echo", my_handler)

        # Simulate a TASK_ACCEPTED event targeting this worker
        envelope = EventEnvelope(
            event_type=DistributedEventType.TASK_ACCEPTED,
            payload={
                "task_id": "task-001",
                "workflow_id": "wf-1",
                "step_id": "step-1",
                "task_type": "echo",
                "task_payload": {},
                "priority": "normal",
                "lease_key": "exec:wf-1:step-1",
                "fencing_token": 1,
                "assigned_worker_id": "w1",
                "attempt": 0,
                "max_attempts": 3,
            },
            source_node_id="scheduler",
        )
        # Deliver directly to the consumer handler
        await worker._on_task_accepted(envelope)

        # Give the dispatch loop a chance to run
        await asyncio.sleep(0.05)

        assert "task-001" in completed
        await worker.stop()
        await transport.stop()

    @pytest.mark.asyncio
    async def test_task_not_for_this_worker_ignored(self):
        worker, transport, _ = _make_worker_stack("w1")
        await transport.start()
        await worker.start()

        received = []
        worker.register_handler("echo", lambda ctx, cb: received.append(ctx) or asyncio.sleep(0))

        envelope = EventEnvelope(
            event_type=DistributedEventType.TASK_ACCEPTED,
            payload={
                "task_id": "t1",
                "workflow_id": "wf1",
                "step_id": "s1",
                "task_type": "echo",
                "task_payload": {},
                "priority": "normal",
                "lease_key": "",
                "fencing_token": 0,
                "assigned_worker_id": "OTHER-worker",  # not w1
                "attempt": 0,
                "max_attempts": 3,
            },
        )
        await worker._on_task_accepted(envelope)
        await asyncio.sleep(0.02)
        assert received == []

        await worker.stop()
        await transport.stop()
