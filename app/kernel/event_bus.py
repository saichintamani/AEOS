"""
AEOS Kernel — Event Bus

The internal publish/subscribe event bus used by all kernel subsystems and
plugins to communicate without direct coupling.

Key properties:
  - Async, in-process (single-node; distributed bus is a v3 concern)
  - Wildcard topic patterns (e.g. "kernel.plugin.*", "agent.*")
  - Fire-and-forget from the publisher's perspective
  - Subscriber failures are caught, logged, and suppressed
  - Queue depth limit prevents runaway memory growth (10 000 events)
  - All events are typed KernelEvent dataclasses

Design rules:
  - Subscriber callbacks MUST be async (coroutine functions)
  - Publishers MUST NOT assume delivery order across subscribers
  - Topics follow the convention: {domain}.{entity}.{past_tense_verb}
"""

from __future__ import annotations

import asyncio
import fnmatch
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Coroutine, Any

from app.core.logger import get_logger
from app.kernel.exceptions import EventBusSaturatedError

__all__ = [
    "KernelEvent",
    "EventBus",
]

log = get_logger(__name__)

_MAX_QUEUE_DEPTH = 10_000


# ── Event Schema ───────────────────────────────────────────────────────────────

@dataclass
class KernelEvent:
    """
    Typed event emitted through the AEOS Kernel Event Bus.

    Topic convention: {domain}.{entity}.{past_tense_action}
    Examples:
        kernel.plugin.loaded
        kernel.resource.granted
        agent.cognitive.step.completed
    """
    topic: str
    source: str
    payload: dict = field(default_factory=dict)
    timestamp: str = ""
    trace_id: str = ""

    def __post_init__(self) -> None:
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "topic": self.topic,
            "source": self.source,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ── Handler type alias ─────────────────────────────────────────────────────────

EventHandler = Callable[[KernelEvent], Coroutine[Any, Any, None]]


# ── Event Bus ──────────────────────────────────────────────────────────────────

class EventBus:
    """
    In-process async publish/subscribe event bus.

    Supports glob-style wildcard topic patterns:
        "kernel.*"         — all direct kernel sub-topics
        "kernel.plugin.*"  — all kernel.plugin.* events
        "*"                — all events (use sparingly)

    Usage:
        bus = EventBus()
        bus.subscribe("kernel.plugin.*", my_handler)
        await bus.emit(KernelEvent(topic="kernel.plugin.loaded", source="kernel", payload={...}))
    """

    def __init__(self, max_queue_depth: int = _MAX_QUEUE_DEPTH) -> None:
        # topic_pattern → list of handlers
        self._subscriptions: dict[str, list[EventHandler]] = defaultdict(list)
        self._max_queue_depth = max_queue_depth
        self._queue_depth: int = 0
        self._dropped_events: int = 0
        self._emitted_total: int = 0
        # Internal queue for decoupled async delivery
        self._queue: asyncio.Queue[KernelEvent] = asyncio.Queue(maxsize=max_queue_depth)
        self._consumer_task: asyncio.Task | None = None
        self._running: bool = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background consumer loop."""
        if self._running:
            return
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop(), name="event-bus-consumer")
        log.info("EventBus started")

    async def stop(self) -> None:
        """Drain the queue and stop the consumer loop."""
        self._running = False
        # Sentinel to unblock queue.get()
        try:
            await asyncio.wait_for(self._drain(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("EventBus drain timeout — some events may be lost")
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        log.info("EventBus stopped", extra={"ctx_emitted": self._emitted_total, "ctx_dropped": self._dropped_events})

    async def _drain(self) -> None:
        """Wait until the queue is empty."""
        while not self._queue.empty():
            await asyncio.sleep(0.05)

    # ── Publish / Subscribe ────────────────────────────────────────────────────

    async def emit(self, event: KernelEvent) -> None:
        """
        Emit an event. Delivery is async and fire-and-forget.

        If the queue is full, the event is dropped and the dropped_events
        counter is incremented. A saturated event is emitted synchronously
        (bypassing the queue) to notify monitoring.
        """
        if not event.timestamp:
            event.timestamp = datetime.now(timezone.utc).isoformat()

        if self._queue.full():
            self._dropped_events += 1
            log.warning(
                "EventBus queue full — event dropped",
                extra={"ctx_topic": event.topic, "ctx_dropped_total": self._dropped_events},
            )
            # Synchronous saturated notification (does not go through queue)
            await self._dispatch_direct(KernelEvent(
                topic="kernel.event_bus.saturated",
                source="event_bus",
                payload={"queue_depth": self._max_queue_depth, "dropped_event_topic": event.topic},
            ))
            return

        await self._queue.put(event)
        self._emitted_total += 1

    def emit_sync(self, event: KernelEvent) -> None:
        """
        Synchronous emit for use from non-async contexts (e.g. signal handlers).
        Uses put_nowait; silently drops if queue is full.
        """
        if not event.timestamp:
            event.timestamp = datetime.now(timezone.utc).isoformat()
        try:
            self._queue.put_nowait(event)
            self._emitted_total += 1
        except asyncio.QueueFull:
            self._dropped_events += 1

    def subscribe(self, topic_pattern: str, handler: EventHandler) -> None:
        """
        Register an async handler for events matching topic_pattern.

        topic_pattern supports glob wildcards:
            "kernel.*", "kernel.plugin.*", "*", "agent.result"

        Registering the same (pattern, handler) pair is a no-op.
        """
        if not topic_pattern:
            raise ValueError("topic_pattern must not be empty")
        if not asyncio.iscoroutinefunction(handler):
            raise ValueError(f"Handler {handler!r} must be a coroutine function (async def)")

        handlers = self._subscriptions[topic_pattern]
        if handler not in handlers:
            handlers.append(handler)
            log.debug("EventBus subscription added", extra={"ctx_pattern": topic_pattern})

    def unsubscribe(self, topic_pattern: str, handler: EventHandler) -> None:
        """Remove a previously registered subscription. No-op if not found."""
        handlers = self._subscriptions.get(topic_pattern, [])
        if handler in handlers:
            handlers.remove(handler)

    # ── Internal dispatch ──────────────────────────────────────────────────────

    async def _consume_loop(self) -> None:
        """Background task that consumes events from the queue and dispatches them."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                await self._dispatch_direct(event)
                self._queue.task_done()
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("EventBus consumer error", extra={"ctx_error": str(exc)})

    async def _dispatch_direct(self, event: KernelEvent) -> None:
        """Find matching subscribers and call each handler concurrently."""
        handlers: list[EventHandler] = []

        for pattern, subs in self._subscriptions.items():
            if fnmatch.fnmatch(event.topic, pattern):
                handlers.extend(subs)

        if not handlers:
            return

        # Deduplicate (same handler may be registered under multiple matching patterns)
        seen: set[int] = set()
        unique_handlers: list[EventHandler] = []
        for h in handlers:
            hid = id(h)
            if hid not in seen:
                seen.add(hid)
                unique_handlers.append(h)

        results = await asyncio.gather(
            *[self._safe_call(h, event) for h in unique_handlers],
            return_exceptions=True,
        )

        for handler, result in zip(unique_handlers, results):
            if isinstance(result, Exception):
                log.warning(
                    "EventBus subscriber raised",
                    extra={"ctx_handler": getattr(handler, "__name__", repr(handler)), "ctx_error": str(result)},
                )

    @staticmethod
    async def _safe_call(handler: EventHandler, event: KernelEvent) -> None:
        try:
            await handler(event)
        except Exception:
            raise  # Caught by gather(return_exceptions=True)

    # ── Introspection ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        return {
            "running": self._running,
            "queue_depth": self._queue.qsize(),
            "max_queue_depth": self._max_queue_depth,
            "subscriptions": {pat: len(handlers) for pat, handlers in self._subscriptions.items()},
            "emitted_total": self._emitted_total,
            "dropped_events": self._dropped_events,
        }
