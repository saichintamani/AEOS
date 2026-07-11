"""
Worker pool — dynamic registration and WorkerView projection.

WorkerPool is updated by heartbeat events via subscribe_to_events().
workers() returns the current list of WorkerViews for use by the scheduler.

Contract: AC-SCHED-001
"""

from __future__ import annotations

import asyncio
import logging

from app.distributed.pool.metrics import WorkerSnapshot
from app.distributed.scheduler.contracts import WorkerView

logger = logging.getLogger(__name__)


class WorkerPool:
    """
    In-memory registry of active worker nodes.

    Thread/task-safe via asyncio.Lock.
    """

    def __init__(self) -> None:
        self._pool: dict[str, WorkerSnapshot] = {}
        self._lock = asyncio.Lock()

    async def register(self, snapshot: WorkerSnapshot) -> None:
        async with self._lock:
            self._pool[snapshot.node_id] = snapshot
            logger.debug("Worker registered: %s", snapshot.node_id)

    async def deregister(self, node_id: str) -> None:
        async with self._lock:
            self._pool.pop(node_id, None)
            logger.debug("Worker deregistered: %s", node_id)

    async def update(self, snapshot: WorkerSnapshot) -> None:
        async with self._lock:
            self._pool[snapshot.node_id] = snapshot

    async def get(self, node_id: str) -> WorkerSnapshot | None:
        async with self._lock:
            return self._pool.get(node_id)

    async def all_snapshots(self) -> list[WorkerSnapshot]:
        async with self._lock:
            return list(self._pool.values())

    def workers(self) -> list[WorkerView]:
        """Return current WorkerView projections (no lock — snapshot read)."""
        return [s.to_worker_view() for s in self._pool.values()]

    async def count(self) -> int:
        async with self._lock:
            return len(self._pool)

    async def subscribe_to_events(self, consumer) -> None:
        """Register handlers on an EventConsumer to auto-update pool from cluster events."""
        from app.distributed.contracts.events import DistributedEventType

        async def _on_heartbeat(envelope) -> None:
            payload = envelope.payload
            node_id = payload.get("node_id", envelope.source_node_id)
            if not node_id:
                return
            snap = await self.get(node_id)
            if snap is None:
                snap = WorkerSnapshot(node_id=node_id)
            snap.in_flight_tasks = payload.get("in_flight_tasks", snap.in_flight_tasks)
            snap.cpu_utilization = payload.get("cpu_utilization", snap.cpu_utilization)
            await self.update(snap)

        async def _on_node_left(envelope) -> None:
            node_id = envelope.payload.get("node_id", "")
            if node_id:
                await self.deregister(node_id)

        consumer.on(DistributedEventType.NODE_JOINED, _on_heartbeat)
        consumer.on(DistributedEventType.NODE_LEFT, _on_node_left)
