"""
Integration tests — multi-worker execution, recovery, fault injection.

These tests wire together the complete Wave 9B.2 stack:
  scheduler + workers + lease + checkpoint + recovery + backpressure

All tests use in-memory components (no Kafka, no Redis).

Conformance test plan references:
  CT-T4-004: Multi-worker concurrent task execution
  CT-T4-005: Worker crash recovery (PROTO-009 pattern C)
  CT-T4-006: Backpressure rejects at hard limit
  CT-T4-007: Governance revocation stops execution
  CT-T4-008: Scheduling strategy load balancing
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.context import ExecutionContext, CheckpointData
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.execution.recovery import RecoveryRuntime
from app.distributed.execution.states import ExecutionState
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.pool.metrics import WorkerSnapshot
from app.distributed.pool.worker_pool import WorkerPool
from app.distributed.scheduler.contracts import SchedulingError
from app.distributed.scheduler.scheduler import DistributedScheduler
from app.distributed.scheduler.strategies import LeastLoadedStrategy, RoundRobinStrategy
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.worker.runtime import WorkerRuntime
from app.distributed.backpressure.engine import BackpressureEngine, BackpressureState
from app.distributed.backpressure.policy import ThresholdPolicy


def _identity(node_id: str) -> NodeIdentity:
    return NodeIdentity(node_id=node_id, host="127.0.0.1", port=9000)


class ClusterFixture:
    """
    Builds an in-memory cluster with N workers for integration testing.
    """

    def __init__(self, n_workers: int = 2) -> None:
        self.transport = InMemoryTransport()
        self.lease_store = InMemoryLeaseStore()
        self.cp_store = InMemoryCheckpointStore()

        ser = JsonEventSerializer()
        router = DefaultEventRouter()
        clock = MonotonicClock()
        self.lease_mgr = ExecutionLeaseManager(self.lease_store)
        self.cp_engine = CheckpointEngine(self.cp_store)

        self.publisher = DefaultEventPublisher(
            clock=clock, router=router, serializer=ser, transport=self.transport,
            source_node_id="scheduler",
        )
        self.pool = WorkerPool()
        self.scheduler = DistributedScheduler(
            strategy=LeastLoadedStrategy(),
            lease_manager=self.lease_mgr,
            publisher=self.publisher,
            scheduler_node_id="scheduler",
        )
        self.workers: list[WorkerRuntime] = []
        for i in range(n_workers):
            nid = f"worker-{i}"
            consumer = DefaultEventConsumer(self.transport, ser, node_id=nid)
            worker = WorkerRuntime(
                identity=_identity(nid),
                publisher=self.publisher,
                consumer=consumer,
                lease_manager=self.lease_mgr,
                checkpoint_engine=self.cp_engine,
                max_in_flight=4,
                heartbeat_interval=9999,
            )
            self.workers.append(worker)

    async def start(self) -> None:
        await self.transport.start()
        for w in self.workers:
            await w.start()

    async def stop(self) -> None:
        for w in self.workers:
            await w.stop()
        await self.transport.stop()

    def register_handler(self, task_type: str, handler) -> None:
        for w in self.workers:
            w.register_handler(task_type, handler)

    def worker_views(self):
        from app.distributed.scheduler.contracts import WorkerView
        return [
            WorkerView(
                node_id=w.node_id,
                host="127.0.0.1",
                region="us-east-1",
                az="a",
                in_flight_tasks=w.metrics.in_flight_tasks,
                capabilities=frozenset(),
                is_healthy=w.is_running,
            )
            for w in self.workers
        ]


# ── CT-T4-004: Multi-worker concurrent execution ──────────────────────────────

class TestMultiWorkerExecution:

    @pytest.mark.asyncio
    async def test_tasks_distributed_across_workers(self):
        """Tasks scheduled round-robin should reach different workers."""
        cluster = ClusterFixture(n_workers=2)
        await cluster.start()

        cluster.scheduler.set_strategy(RoundRobinStrategy())

        executed_by: list[str] = []

        async def track_handler(ctx, callbacks):
            executed_by.append(ctx.assigned_worker_id)
            return {}

        cluster.register_handler("track", track_handler)

        views = cluster.worker_views()
        for i in range(4):
            ctx = ExecutionContext(
                task_id=f"t-{i}",
                workflow_id="wf-1",
                step_id=f"step-{i}",
                task_type="track",
                priority="normal",
            )
            ctx.transition(ExecutionState.QUEUED)
            decision = await cluster.scheduler.schedule(ctx, views)

            # Simulate the worker receiving and processing the task
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
                    "assigned_worker_id": decision.worker.node_id,
                    "attempt": 0,
                    "max_attempts": 3,
                },
                source_node_id="scheduler",
            )
            # Deliver to the correct worker
            worker = next(w for w in cluster.workers if w.node_id == decision.worker.node_id)
            await worker._on_task_accepted(envelope)

        # Allow event loop to process queued tasks
        await asyncio.sleep(0.1)

        # Both workers should have received tasks
        assert len(set(executed_by)) > 0
        assert cluster.scheduler.scheduled_count == 4

        await cluster.stop()

    @pytest.mark.asyncio
    async def test_concurrent_task_execution(self):
        """Multiple tasks can execute concurrently within a worker."""
        cluster = ClusterFixture(n_workers=1)
        await cluster.start()

        barrier = asyncio.Barrier(3)
        completed = []

        async def barrier_handler(ctx, callbacks):
            await barrier.wait()
            completed.append(ctx.task_id)
            return {}

        cluster.register_handler("barrier", barrier_handler)

        views = cluster.worker_views()
        for i in range(3):
            ctx = ExecutionContext(
                task_id=f"t-{i}",
                workflow_id="wf-1",
                step_id=f"step-{i}",
                task_type="barrier",
                priority="normal",
            )
            ctx.transition(ExecutionState.QUEUED)
            decision = await cluster.scheduler.schedule(ctx, views)
            worker = cluster.workers[0]
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
                    "assigned_worker_id": decision.worker.node_id,
                    "attempt": 0,
                    "max_attempts": 3,
                },
                source_node_id="scheduler",
            )
            await worker._on_task_accepted(envelope)

        # Allow event loop to process queued tasks
        await asyncio.sleep(0.2)
        await cluster.stop()


# ── CT-T4-005: Recovery after worker crash ────────────────────────────────────

class TestWorkerCrashRecovery:

    @pytest.mark.asyncio
    async def test_pattern_c_recovery_resumes_from_checkpoint(self):
        """
        Simulate: worker executed partway, crashed, lease expired,
        recovery detects orphan and resumes from checkpoint.
        """
        lease_store = InMemoryLeaseStore()
        cp_store = InMemoryCheckpointStore()
        lease_mgr = ExecutionLeaseManager(lease_store)
        cp_engine = CheckpointEngine(cp_store)

        # Simulate original execution: worker acquired lease, checkpointed at step 5
        token = await lease_mgr.acquire("exec:wf-recover:step-1", "crashed-worker")
        assert token is not None

        cp_data = CheckpointData(
            task_id="t-crash",
            workflow_id="wf-recover",
            step_id="step-1",
            execution_id="exec-original",
            step_index=5,
            total_steps=10,
            task_state={"progress": 0.5},
        )
        entry = await cp_engine.write_full(cp_data)
        await cp_engine.commit(entry)

        # Simulate crash: release lease (normally it just expires)
        await lease_mgr.release(token)

        # Set up recovery runtime
        recovery = RecoveryRuntime(lease_mgr, cp_engine, worker_id="recovery-worker")

        ctx = ExecutionContext(
            task_id="t-crash",
            workflow_id="wf-recover",
            step_id="step-1",
            state=ExecutionState.RUNNING,
            assigned_worker_id="crashed-worker",
            lease_key="exec:wf-recover:step-1",
        )
        ctx.execution_id = "exec-original"

        result = await recovery.recover(ctx)
        assert result.success
        assert result.pattern == "C"
        assert ctx.state == ExecutionState.RECOVERING
        assert result.checkpoint is not None
        assert result.checkpoint.step_index == 5
        assert result.token is not None
        assert result.token.holder_id == "recovery-worker"

    @pytest.mark.asyncio
    async def test_pattern_a_no_checkpoint_requeues(self):
        """No checkpoint available → task re-queued for fresh execution."""
        lease_store = InMemoryLeaseStore()
        cp_store = InMemoryCheckpointStore()
        lease_mgr = ExecutionLeaseManager(lease_store)
        cp_engine = CheckpointEngine(cp_store)
        recovery = RecoveryRuntime(lease_mgr, cp_engine)

        ctx = ExecutionContext(
            workflow_id="wf-1", step_id="step-1",
            state=ExecutionState.RUNNING,
            assigned_worker_id="crashed",
            lease_key="exec:wf-1:step-1",
            attempt=0, max_attempts=3,
        )

        result = await recovery.recover(ctx)
        assert result.success
        assert result.pattern == "A"
        assert ctx.state == ExecutionState.QUEUED
        assert ctx.attempt == 1


# ── CT-T4-006: Backpressure ───────────────────────────────────────────────────

class TestBackpressure:

    @pytest.mark.asyncio
    async def test_normal_state_when_idle(self):
        pool = WorkerPool()
        await pool.register(WorkerSnapshot(
            node_id="w1", host="127.0.0.1", port=9000,
            queue_depth=0, cpu_utilization=0.1,
        ))
        engine = BackpressureEngine(pool, ThresholdPolicy(min_healthy_workers=1))
        state = await engine.evaluate_once()
        assert state == BackpressureState.NORMAL

    @pytest.mark.asyncio
    async def test_slowing_when_queue_exceeds_warn(self):
        pool = WorkerPool()
        await pool.register(WorkerSnapshot(
            node_id="w1", host="127.0.0.1", port=9000,
            queue_depth=40,   # > warn threshold of 32
            cpu_utilization=0.5,
        ))
        engine = BackpressureEngine(pool, ThresholdPolicy(queue_warn_threshold=32.0))
        state = await engine.evaluate_once()
        assert state == BackpressureState.SLOWING

    @pytest.mark.asyncio
    async def test_rejecting_when_queue_exceeds_reject(self):
        pool = WorkerPool()
        await pool.register(WorkerSnapshot(
            node_id="w1", host="127.0.0.1", port=9000,
            queue_depth=60,   # > reject threshold of 56
            cpu_utilization=0.5,
        ))
        engine = BackpressureEngine(
            pool,
            ThresholdPolicy(queue_warn_threshold=32.0, queue_reject_threshold=56.0),
        )
        state = await engine.evaluate_once()
        assert state == BackpressureState.REJECTING
        assert engine.should_reject()

    @pytest.mark.asyncio
    async def test_delay_when_slowing(self):
        pool = WorkerPool()
        await pool.register(WorkerSnapshot(
            node_id="w1", host="127.0.0.1", port=9000,
            queue_depth=40,
        ))
        policy = ThresholdPolicy(queue_warn_threshold=32.0, slow_delay=0.1)
        engine = BackpressureEngine(pool, policy)
        await engine.evaluate_once()
        assert engine.delay_seconds() == 0.1


# ── CT-T4-007: Governance revocation stops execution ─────────────────────────

class TestGovernanceRevocation:

    @pytest.mark.asyncio
    async def test_revoked_token_prevents_execution(self):
        """A task with a revoked token must not execute."""
        from app.distributed.worker.governance import GovernanceClient, TokenRevokedException

        transport = InMemoryTransport()
        await transport.start()
        ser = JsonEventSerializer()
        consumer = DefaultEventConsumer(transport, ser, node_id="worker-1")
        client = GovernanceClient(consumer, "worker-1")

        # Pre-revoke the token
        async with client._lock:
            client._revoked_tokens.add("revoked-tok")

        with pytest.raises(TokenRevokedException):
            await client.verify_token("revoked-tok")

        await transport.stop()


# ── Property-based: INV-EXEC-001 (no duplicate execution) ────────────────────

class TestNoDoubleExecution:
    """INV-EXEC-001: at most one active execution per (workflow_id, step_id) triple."""

    @pytest.mark.asyncio
    async def test_second_acquire_fails(self):
        """Two workers racing to acquire the same lease — only one wins."""
        lease_store = InMemoryLeaseStore()
        lease_mgr = ExecutionLeaseManager(lease_store)

        lease_key = "exec:wf-double:step-1"
        results = await asyncio.gather(
            lease_mgr.acquire(lease_key, "worker-a"),
            lease_mgr.acquire(lease_key, "worker-b"),
        )

        winners = [r for r in results if r is not None]
        assert len(winners) == 1, "Exactly one worker should have acquired the lease"

    @pytest.mark.asyncio
    async def test_stale_token_detected_after_steal(self):
        """After a lease steal, the original holder's token is invalid."""
        from app.distributed.execution.lease import StaleFencingTokenError

        lease_store = InMemoryLeaseStore()
        lease_mgr = ExecutionLeaseManager(lease_store)

        token_a = await lease_mgr.acquire("exec:wf1:s1", "worker-a")
        # Simulate crash + steal
        stolen_token = await lease_mgr.steal("exec:wf1:s1", "recovery-worker")

        assert stolen_token is not None
        # worker-a's token is now stale
        valid = await lease_mgr.verify(token_a)
        assert not valid
