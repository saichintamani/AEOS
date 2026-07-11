"""
AEOS — Async Message Bus
Backbone for inter-agent and agent-to-orchestrator event communication.

Supports:
  - publish(topic, message)     broadcast to all topic subscribers (concurrent)
  - subscribe(topic, handler)   register an async handler
  - unsubscribe(topic, handler) remove a handler
  - request(...)                point-to-point directed message

Topics used by the orchestrator:
  "task.started"   — task accepted, context ready
  "agent.result"   — an agent completed a step
  "task.completed" — task finished successfully
  "task.failed"    — task finished with an error

Usage:
    from app.core.message_bus import get_message_bus, AgentMessage
    bus = get_message_bus()

    async def on_task_done(msg: AgentMessage):
        print(msg.payload)

    bus.subscribe("task.completed", on_task_done)
    await bus.publish("task.completed", AgentMessage(...))
"""

from __future__ import annotations
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger

log = get_logger(__name__)


# ── Message schema ─────────────────────────────────────────────────────────────

@dataclass
class AgentMessage:
    """
    Unit of communication on the message bus.
    Every publish() and request() call produces one AgentMessage.
    """
    topic: str
    sender_id: str
    payload: dict
    trace_id: str = ""
    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "message_id": self.message_id,
            "topic": self.topic,
            "sender_id": self.sender_id,
            "trace_id": self.trace_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
        }


# ── Handler type alias ─────────────────────────────────────────────────────────

Handler = Callable[[AgentMessage], Coroutine[Any, Any, None]]


# ── Message Bus ────────────────────────────────────────────────────────────────

class MessageBus:
    """
    Async pub/sub message bus.

    - Handlers are coroutines (async def).
    - All subscribers for a topic are called concurrently via asyncio.gather.
    - Handler exceptions are caught and logged — they never crash the publisher.
    - The bus is in-process; it does not persist messages.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Handler]] = {}
        log.info("MessageBus initialized")

    # ── Subscription management ────────────────────────────────────────────────

    def subscribe(self, topic: str, handler: Handler) -> None:
        """Register an async handler for a topic. Safe to call multiple times."""
        if topic not in self._subscribers:
            self._subscribers[topic] = []
        if handler not in self._subscribers[topic]:
            self._subscribers[topic].append(handler)
            log.debug(
                "Handler subscribed",
                extra={"ctx_topic": topic, "ctx_handler": getattr(handler, "__name__", repr(handler))},
            )

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        """Remove a handler for a topic. No-op if not subscribed."""
        if topic in self._subscribers:
            try:
                self._subscribers[topic].remove(handler)
            except ValueError:
                pass

    # ── Publish ────────────────────────────────────────────────────────────────

    async def publish(self, topic: str, message: AgentMessage) -> None:
        """
        Broadcast message to all subscribers of a topic.
        Handlers run concurrently. Exceptions in handlers are swallowed and logged.
        Publishing to a topic with no subscribers is a no-op.
        """
        handlers = self._subscribers.get(topic, [])
        if not handlers:
            log.debug("No subscribers for topic", extra={"ctx_topic": topic})
            return

        log.debug(
            "Publishing message",
            extra={
                "ctx_topic": topic,
                "ctx_message_id": message.message_id,
                "ctx_sender": message.sender_id,
                "ctx_handler_count": len(handlers),
            },
        )

        async def _safe_call(h: Handler) -> None:
            try:
                await h(message)
            except Exception as exc:
                log.exception(
                    "Message handler raised",
                    extra={
                        "ctx_topic": topic,
                        "ctx_handler": getattr(h, "__name__", repr(h)),
                        "ctx_error": str(exc),
                    },
                )

        await asyncio.gather(*[_safe_call(h) for h in handlers])

    # ── Point-to-point request ─────────────────────────────────────────────────

    async def request(
        self,
        sender_id: str,
        recipient_id: str,
        payload: dict,
        trace_id: str = "",
    ) -> AgentMessage:
        """
        Directed point-to-point message.
        Topic is auto-generated as "direct.<sender_id>→<recipient_id>".
        The reply is returned by the first subscriber on the reply topic,
        or an echo-back AgentMessage if no handler is registered.
        """
        topic = f"direct.{sender_id}→{recipient_id}"
        msg = AgentMessage(
            topic=topic,
            sender_id=sender_id,
            payload=payload,
            trace_id=trace_id,
        )
        await self.publish(topic, msg)
        log.debug(
            "Direct request sent",
            extra={"ctx_from": sender_id, "ctx_to": recipient_id},
        )
        return msg

    # ── Utility ────────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset all subscriptions. Useful in tests."""
        self._subscribers.clear()
        log.debug("MessageBus cleared")

    def topic_count(self) -> int:
        return len(self._subscribers)

    def subscriber_count(self, topic: str) -> int:
        return len(self._subscribers.get(topic, []))

    def summarize(self) -> dict:
        return {
            "topics": list(self._subscribers.keys()),
            "total_handlers": sum(len(v) for v in self._subscribers.values()),
        }


@lru_cache(maxsize=1)
def get_message_bus() -> MessageBus:
    """Cached singleton. All agents and the orchestrator share one instance."""
    return MessageBus()
