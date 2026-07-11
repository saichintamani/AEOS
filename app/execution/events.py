"""
AEOS Distributed Execution Engine — Execution Events

Fine-grained event system for the execution layer. Decoupled from the
kernel EventBus — execution events are higher-frequency and node-scoped.
The ExecutionEventBus can be bridged to the KernelEventBus if needed.

Subscribers register per event type; async handlers are awaited,
sync handlers are called directly.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger

__all__ = [
    "ExecutionEventType",
    "ExecutionEvent",
    "ExecutionEventBus",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionEventType(str, Enum):
    # Workflow lifecycle
    WORKFLOW_STARTED    = "execution.workflow.started"
    WORKFLOW_COMPLETED  = "execution.workflow.completed"
    WORKFLOW_FAILED     = "execution.workflow.failed"
    WORKFLOW_CANCELLED  = "execution.workflow.cancelled"

    # Node lifecycle
    NODE_QUEUED         = "execution.node.queued"
    NODE_STARTED        = "execution.node.started"
    NODE_COMPLETED      = "execution.node.completed"
    NODE_FAILED         = "execution.node.failed"
    NODE_TIMED_OUT      = "execution.node.timed_out"
    NODE_SKIPPED        = "execution.node.skipped"
    NODE_RETRIED        = "execution.node.retried"

    # Checkpoint
    CHECKPOINT_SAVED    = "execution.checkpoint.saved"
    CHECKPOINT_RESTORED = "execution.checkpoint.restored"

    # Worker
    WORKER_ASSIGNED     = "execution.worker.assigned"
    WORKER_RELEASED     = "execution.worker.released"

    # Circuit breaker
    CIRCUIT_BREAKER_OPEN   = "execution.circuit_breaker.open"
    CIRCUIT_BREAKER_CLOSED = "execution.circuit_breaker.closed"

    # Replay
    REPLAY_STARTED      = "execution.replay.started"
    REPLAY_COMPLETED    = "execution.replay.completed"


EventHandler = Callable[["ExecutionEvent"], Any]


@dataclass
class ExecutionEvent:
    """A single execution-layer event."""
    event_type: ExecutionEventType
    workflow_id: str
    node_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    timestamp: str = field(default_factory=_now)

    def with_node(self, node_id: str, **payload_kwargs: Any) -> "ExecutionEvent":
        """Convenience: return a copy with node_id and extra payload."""
        return ExecutionEvent(
            event_type=self.event_type,
            workflow_id=self.workflow_id,
            node_id=node_id,
            payload={**self.payload, **payload_kwargs},
            trace_id=self.trace_id,
        )


class ExecutionEventBus:
    """
    Lightweight pub/sub bus for execution events.

    - Supports both sync and async handlers
    - Async handlers are awaited; sync handlers are called inline
    - Errors in handlers are logged and swallowed (never crash the pipeline)
    - Wildcard subscription via ExecutionEventType values or "*" for all events
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._emit_count: int = 0
        self._error_count: int = 0

    # ── Subscription ──────────────────────────────────────────────────────────

    def subscribe(
        self,
        event_type: ExecutionEventType | str,
        handler: EventHandler,
    ) -> None:
        """Register a handler for a specific event type (or "*" for all)."""
        key = event_type if isinstance(event_type, str) else event_type.value
        if handler not in self._handlers[key]:
            self._handlers[key].append(handler)

    def unsubscribe(
        self,
        event_type: ExecutionEventType | str,
        handler: EventHandler,
    ) -> None:
        """Remove a previously registered handler."""
        key = event_type if isinstance(event_type, str) else event_type.value
        handlers = self._handlers[key]
        if handler in handlers:
            handlers.remove(handler)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe to every event type."""
        self.subscribe("*", handler)

    # ── Emission ──────────────────────────────────────────────────────────────

    async def emit(self, event: ExecutionEvent) -> None:
        """Async emit — awaits async handlers, calls sync handlers inline."""
        self._emit_count += 1
        handlers = (
            self._handlers.get(event.event_type.value, [])
            + self._handlers.get("*", [])
        )
        for handler in handlers:
            try:
                result = handler(event)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                self._error_count += 1
                log.warning(
                    "ExecutionEventBus handler error",
                    extra={
                        "ctx_event": event.event_type.value,
                        "ctx_workflow_id": event.workflow_id,
                        "ctx_error": str(exc),
                    },
                )

    def emit_sync(self, event: ExecutionEvent) -> None:
        """
        Synchronous emit — only calls sync handlers; skips async ones.

        Use only when no event loop is available (e.g., __init__ code).
        """
        self._emit_count += 1
        handlers = (
            self._handlers.get(event.event_type.value, [])
            + self._handlers.get("*", [])
        )
        for handler in handlers:
            if inspect.iscoroutinefunction(handler):
                continue  # skip async in sync context
            try:
                handler(event)
            except Exception as exc:
                self._error_count += 1
                log.warning(
                    "ExecutionEventBus sync handler error",
                    extra={"ctx_event": event.event_type.value, "ctx_error": str(exc)},
                )

    # ── Introspection ─────────────────────────────────────────────────────────

    def handler_count(self) -> int:
        return sum(len(v) for v in self._handlers.values())

    def summarize(self) -> dict[str, Any]:
        return {
            "subscriptions": {k: len(v) for k, v in self._handlers.items() if v},
            "emit_count": self._emit_count,
            "error_count": self._error_count,
        }
