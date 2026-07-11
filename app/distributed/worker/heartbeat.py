"""
Heartbeat service.

Publishes NODE_JOINED events (with heartbeat=True payload) at a configured
interval. Recipients use these to reset their missed-heartbeat counters
(PROTO-003 failure detection).

Protocol: PROTO-003
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from app.distributed.contracts.events import DistributedEventType, EventEnvelope, EventPublisher

logger = logging.getLogger(__name__)


class HeartbeatService:
    """
    Periodically emits NODE_JOINED events so the cluster manager can reset
    missed-heartbeat counters for this node.

    Pass a metrics_provider callable that returns the current metrics dict.
    """

    def __init__(
        self,
        publisher: EventPublisher,
        node_id: str,
        metrics_provider: Callable[[], dict],
        *,
        interval_seconds: float = 10.0,
    ) -> None:
        self._publisher = publisher
        self._node_id = node_id
        self._metrics_provider = metrics_provider
        self._interval = interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False
        self._beat_count = 0

    @property
    def beat_count(self) -> int:
        return self._beat_count

    async def beat(self) -> None:
        """Emit a single heartbeat event immediately."""
        metrics = self._metrics_provider()
        await self._publisher.publish(EventEnvelope(
            event_type=DistributedEventType.NODE_JOINED,
            payload={
                "node_id": self._node_id,
                "heartbeat": True,
                **metrics,
            },
            source_node_id=self._node_id,
        ))
        self._beat_count += 1

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name=f"heartbeat-{self._node_id}")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.beat()
            except Exception:
                logger.exception("Heartbeat error on %s", self._node_id)
            await asyncio.sleep(self._interval)
