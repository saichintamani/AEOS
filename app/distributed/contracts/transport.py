"""
Transport contract — the interface every message transport must satisfy.

A transport is responsible only for moving bytes between producers and
consumers. It has no knowledge of message schemas, routing, or business
logic. Three implementations exist: KafkaTransport, InMemoryTransport,
and TestTransport (see app/distributed/transport/).

Contract: AC-IFACE-003 (Kafka message schema enforced at serialiser layer above)
ADR: ADR-002 (consumer group separation enforced by KafkaTransport)
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Coroutine


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Transport Message ─────────────────────────────────────────────────────────

@dataclass
class TransportMessage:
    """
    Raw message as seen by the transport layer.

    Headers carry cross-cutting concerns (trace context, schema version)
    without coupling the transport to any domain schema.
    """
    topic: str
    payload: bytes
    message_id: str = field(default_factory=_new_id)
    produced_at: str = field(default_factory=_now_iso)
    headers: dict[str, str] = field(default_factory=dict)
    partition: int | None = None
    offset: int | None = None
    key: bytes | None = None

    # W3C trace context — propagated across service boundaries
    trace_id: str | None = None
    span_id: str | None = None


# ── Handler type alias ────────────────────────────────────────────────────────

MessageHandler = Callable[[TransportMessage], Coroutine[Any, Any, None]]


# ── Transport ABC ─────────────────────────────────────────────────────────────

class MessageTransport(ABC):
    """
    Abstract message transport.

    Implementations: KafkaTransport, InMemoryTransport, TestTransport.

    Lifecycle::
        await transport.start()
        # ... use ...
        await transport.stop()

    Contract clauses satisfied by implementations:
        AC-IFACE-003: schema_version header injected by KafkaTransport
        INV-DIST-001: consumer group IDs enforced by KafkaTransport
    """

    @abstractmethod
    async def start(self) -> None:
        """Connect to the broker and prepare internal state."""

    @abstractmethod
    async def stop(self) -> None:
        """Drain in-flight messages, close connections gracefully."""

    @abstractmethod
    async def publish(
        self,
        message: TransportMessage,
        *,
        wait_for_ack: bool = True,
    ) -> None:
        """
        Publish a message to the given topic.

        Args:
            message: The message to publish. ``message.topic`` determines
                     the destination.
            wait_for_ack: When True, await broker acknowledgement before
                          returning. Set to False for fire-and-forget at
                          reduced durability.
        """

    @abstractmethod
    async def subscribe(
        self,
        topics: list[str],
        group_id: str,
        handler: MessageHandler,
        *,
        auto_commit: bool = False,
    ) -> str:
        """
        Subscribe to one or more topics.

        Args:
            topics:     Topic names to consume from.
            group_id:   Consumer group identifier. The caller is responsible
                        for using the correct group ID (shared vs per-worker).
                        See ADR-002 and KafkaConsumerFactory for correct usage.
            handler:    Async coroutine called for each received message.
            auto_commit: When False (default), the transport calls
                         ``commit(subscription_id, message)`` after the
                         handler completes without raising. This implements
                         the at-least-once contract (AC-EXEC-002).

        Returns:
            subscription_id: Opaque string identifying this subscription;
                             pass to ``unsubscribe()`` or ``commit()``.
        """

    @abstractmethod
    async def unsubscribe(self, subscription_id: str) -> None:
        """Cancel a subscription and stop delivering messages."""

    @abstractmethod
    async def commit(self, subscription_id: str, message: TransportMessage) -> None:
        """
        Explicitly commit the offset for a processed message.

        Must be called ONLY after Phase 2 checkpoint completes.
        Contract: AC-EXEC-002 (offset committed after checkpoint, not before).
        """

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the transport is connected and healthy."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True after ``start()`` and before ``stop()``."""
