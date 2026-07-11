"""
Wave 9B.4.7 — Runtime Telemetry Bus

Every component in AEOS publishes structured events here.
Subscribers receive typed events for monitoring, metrics, and debugging.

TelemetryEvent  — structured event envelope
TelemetryBus    — async pub/sub bus (in-process, no network round-trip)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

EventHandler = Callable[["TelemetryEvent"], Coroutine[Any, Any, None]]


class TelemetryEventType(str, Enum):
    # Task lifecycle
    TASK_SUBMITTED        = "task.submitted"
    TASK_STARTED          = "task.started"
    TASK_COMPLETED        = "task.completed"
    TASK_FAILED           = "task.failed"
    TASK_RETRIED          = "task.retried"
    TASK_CANCELLED        = "task.cancelled"

    # Worker lifecycle
    NODE_JOINED           = "node.joined"
    NODE_LEFT             = "node.left"
    WORKER_OVERLOADED     = "worker.overloaded"
    WORKER_CRASHED        = "worker.crashed"
    WORKER_RECOVERED      = "worker.recovered"

    # Scheduling / decisions
    DECISION_MADE         = "decision.made"
    GOVERNANCE_REJECTED   = "governance.rejected"
    POLICY_UPDATED        = "policy.updated"

    # Execution infrastructure
    LEASE_ACQUIRED        = "lease.acquired"
    LEASE_LOST            = "lease.lost"
    CHECKPOINT_SAVED      = "checkpoint.saved"
    EXECUTION_RECOVERED   = "execution.recovered"

    # Resource management
    RESOURCE_PRESSURE     = "resource.pressure"
    AUTOSCALE_TRIGGERED   = "autoscale.triggered"
    QUEUE_OVERFLOW        = "queue.overflow"

    # Optimization / learning
    LEARNING_UPDATED      = "learning.updated"
    OPTIMIZATION_APPLIED  = "optimization.applied"

    # Plugin / extension
    PLUGIN_LOADED         = "plugin.loaded"
    PLUGIN_FAILED         = "plugin.failed"


@dataclass
class TelemetryEvent:
    event_type: TelemetryEventType
    source: str                                    # component that emitted this
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    correlation_id: str = ""                       # task_id / workflow_id for tracing
    worker_id: str = ""


class TelemetryBus:
    """
    In-process async pub/sub telemetry bus.

    Handlers are fire-and-forget: exceptions are logged, not propagated.
    Subscription returns an ID usable for unsubscribing.
    """

    def __init__(self) -> None:
        self._handlers: dict[TelemetryEventType | None, list[tuple[str, EventHandler]]] = {}
        self._all_handlers: list[tuple[str, EventHandler]] = []
        self._counter = 0
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        event_type: TelemetryEventType | None,
        handler: EventHandler,
    ) -> str:
        """Subscribe to a specific event type, or None to receive all events."""
        async with self._lock:
            self._counter += 1
            sub_id = f"sub-{self._counter}"
            if event_type is None:
                self._all_handlers.append((sub_id, handler))
            else:
                if event_type not in self._handlers:
                    self._handlers[event_type] = []
                self._handlers[event_type].append((sub_id, handler))
            return sub_id

    async def unsubscribe(self, sub_id: str) -> None:
        async with self._lock:
            self._all_handlers = [(sid, h) for sid, h in self._all_handlers if sid != sub_id]
            for et in self._handlers:
                self._handlers[et] = [(sid, h) for sid, h in self._handlers[et] if sid != sub_id]

    async def publish(self, event: TelemetryEvent) -> None:
        async with self._lock:
            specific = list(self._handlers.get(event.event_type, []))
            all_h = list(self._all_handlers)

        handlers = specific + all_h
        for _, handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "TelemetryBus: handler error for %s from %s",
                    event.event_type, event.source,
                )

    def emit(self, event: TelemetryEvent) -> asyncio.Task:
        """Fire-and-forget from synchronous contexts."""
        return asyncio.create_task(self.publish(event))

    async def drain(self) -> None:
        """Yield control so any pending tasks can complete (useful in tests)."""
        await asyncio.sleep(0)
