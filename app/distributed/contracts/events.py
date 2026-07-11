"""
Distributed event contracts.

An EventEnvelope wraps any domain payload for transmission across the
cluster. The EventSerializer converts envelopes to/from bytes. The
EventRouter decides which topic an event belongs to. EventPublisher and
EventConsumer are the send/receive interfaces used by higher layers.

Protocol: PROTO-006 (task dispatch via events)
Protocol: PROTO-015 (RBAC revocation events)
Contract: AC-IFACE-003 (Kafka message schema), AC-OBS-003 (trace propagation)
ADR: ADR-002 (consumer group separation — enforced at EventConsumer layer)
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Distributed Event Types ───────────────────────────────────────────────────

class DistributedEventType(str, Enum):
    """
    All event types that flow over the distributed event fabric.

    Task events → aeos.tasks.{priority} (competing consumer)
    Cluster/governance events → aeos.events.{category} (fan-out per-worker)

    INV-DIST-001: task topics use shared group; event topics use per-worker group.
    """
    # Task lifecycle (→ aeos.tasks.{priority})
    TASK_SUBMITTED   = "task.submitted"
    TASK_ACCEPTED    = "task.accepted"
    TASK_COMPLETED   = "task.completed"
    TASK_FAILED      = "task.failed"
    TASK_CANCELLED   = "task.cancelled"
    TASK_RETRY       = "task.retry"

    # Cluster events (→ aeos.events.cluster, fan-out)
    NODE_JOINED      = "cluster.node.joined"
    NODE_LEFT        = "cluster.node.left"
    NODE_SUSPECTED   = "cluster.node.suspected"
    NODE_FAILED      = "cluster.node.failed"
    LEADER_CHANGED   = "cluster.leader.changed"

    # Governance events (→ aeos.events.governance, fan-out)
    TOKEN_ISSUED     = "governance.token.issued"
    TOKEN_REVOKED    = "governance.token.revoked"
    POLICY_CHANGED   = "governance.policy.changed"
    RBAC_REVOKED     = "governance.rbac.revoked"

    # Capability events (→ aeos.events.capability, fan-out)
    CAPABILITY_REGISTERED   = "capability.registered"
    CAPABILITY_DEREGISTERED = "capability.deregistered"
    CAPABILITY_DEGRADED     = "capability.degraded"


# ── Topic → Role Mapping ──────────────────────────────────────────────────────

TASK_TOPICS: frozenset[str] = frozenset({
    "aeos.tasks.critical",
    "aeos.tasks.high",
    "aeos.tasks.normal",
    "aeos.tasks.low",
    "aeos.tasks.batch",
    "aeos.tasks.dead_letter",
})

EVENT_TOPICS: frozenset[str] = frozenset({
    "aeos.events.cluster",
    "aeos.events.governance",
    "aeos.events.capability",
})

SHARED_CONSUMER_GROUP = "aeos-workers"


def per_worker_group_id(node_id: str) -> str:
    """
    Return the per-worker consumer group ID for event (fan-out) topics.

    ADR-002: event topics use per-worker group so every worker receives
    every event. Task topics use the shared 'aeos-workers' group.
    """
    return f"aeos-worker-{node_id}"


def consumer_group_for_topic(topic: str, node_id: str) -> str:
    """Return the correct consumer group for a given topic."""
    if topic in TASK_TOPICS:
        return SHARED_CONSUMER_GROUP
    if topic in EVENT_TOPICS:
        return per_worker_group_id(node_id)
    # Unknown topics default to per-worker (fan-out — safer default)
    return per_worker_group_id(node_id)


# ── Event Envelope ────────────────────────────────────────────────────────────

@dataclass
class EventEnvelope:
    """
    Wrapper for any distributed event payload.

    The envelope provides the cross-cutting fields required by
    AC-IFACE-003 (message_id, produced_at, schema_version) plus
    W3C trace context (AC-OBS-003).

    sequence_nanos: Nanosecond logical clock from originating worker.
    Used for cross-topic ordering (§9.3 v1.1 spec).
    """
    event_type: DistributedEventType
    payload: dict[str, Any]

    message_id: str = field(default_factory=_new_id)
    produced_at: str = field(default_factory=_now_iso)
    schema_version: int = 1
    sequence_nanos: int = 0          # set by EventPublisher from DistributedClock

    # Source identity
    source_node_id: str = ""
    source_service: str = ""

    # W3C trace context — AC-OBS-003
    trace_id: str | None = None
    span_id: str | None = None

    # Correlation
    workflow_id: str | None = None
    task_id: str | None = None

    def to_headers(self) -> dict[str, str]:
        """Return transport-level headers derived from the envelope."""
        headers = {
            "message_id": self.message_id,
            "event_type": self.event_type.value,
            "schema_version": str(self.schema_version),
            "sequence_nanos": str(self.sequence_nanos),
            "source_node_id": self.source_node_id,
        }
        if self.trace_id:
            headers["traceparent"] = f"00-{self.trace_id}-{self.span_id or '0' * 16}-01"
        if self.workflow_id:
            headers["workflow_id"] = self.workflow_id
        if self.task_id:
            headers["task_id"] = self.task_id
        return headers


# ── Serialiser ABC ────────────────────────────────────────────────────────────

class EventSerializer(ABC):
    """Convert EventEnvelopes to/from bytes for transport."""

    @abstractmethod
    def serialize(self, envelope: EventEnvelope) -> bytes:
        """Encode an envelope to bytes."""

    @abstractmethod
    def deserialize(self, data: bytes) -> EventEnvelope:
        """Decode bytes to an envelope. Raises ValueError on schema mismatch."""


# ── Router ABC ────────────────────────────────────────────────────────────────

class EventRouter(ABC):
    """Determine the Kafka topic for a given event."""

    @abstractmethod
    def route(self, envelope: EventEnvelope) -> str:
        """Return the topic name this envelope should be published to."""

    @abstractmethod
    def partition_key(self, envelope: EventEnvelope) -> bytes | None:
        """
        Return the Kafka partition key, or None for round-robin.

        Workflow events should use workflow_id as the key so that all
        events for a workflow land on the same partition (ordering).
        """


# ── Publisher ABC ─────────────────────────────────────────────────────────────

class EventPublisher(ABC):
    """Send EventEnvelopes to the distributed event fabric."""

    @abstractmethod
    async def publish(
        self,
        envelope: EventEnvelope,
        *,
        wait_for_ack: bool = True,
    ) -> None:
        """
        Publish an event.

        Implementations must:
        1. Set envelope.sequence_nanos from the DistributedClock
        2. Route the event via EventRouter
        3. Serialize via EventSerializer
        4. Deliver via MessageTransport
        """

    @abstractmethod
    async def publish_many(self, envelopes: list[EventEnvelope]) -> None:
        """Batch-publish multiple events atomically where supported."""


# ── Consumer ABC ─────────────────────────────────────────────────────────────

class EventConsumer(ABC):
    """Receive EventEnvelopes from the distributed event fabric."""

    @abstractmethod
    async def subscribe(
        self,
        event_types: list[DistributedEventType],
        handler: "EventHandler",
        node_id: str,
    ) -> str:
        """
        Subscribe to a set of event types.

        The implementation selects the correct consumer group:
        - Task topics → SHARED_CONSUMER_GROUP
        - Event topics → per_worker_group_id(node_id)
        This enforces INV-DIST-001 at the consumer layer.

        Returns a subscription_id for later unsubscription.
        """

    @abstractmethod
    async def unsubscribe(self, subscription_id: str) -> None:
        """Cancel a subscription."""

    @abstractmethod
    async def start(self) -> None:
        """Begin consuming messages."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop consuming and release resources."""


# ── Handler type alias ────────────────────────────────────────────────────────

from typing import Callable, Coroutine
EventHandler = Callable[[EventEnvelope], Coroutine[Any, Any, None]]
