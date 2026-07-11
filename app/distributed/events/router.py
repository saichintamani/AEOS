"""
Default event router.

Maps DistributedEventType → Kafka topic using the topic taxonomy from §8.1.
Priority task events fan to aeos.tasks.{priority} topics.
Dead-letter routing fires after max retries are exceeded.

Contract: AC-IFACE-003
ADR: ADR-002
"""

from __future__ import annotations

from app.distributed.contracts.events import DistributedEventType, EventEnvelope, EventRouter


# Task topic by priority level
_TASK_PRIORITY_TOPICS: dict[str, str] = {
    "critical": "aeos.tasks.critical",
    "high":     "aeos.tasks.high",
    "normal":   "aeos.tasks.normal",
    "low":      "aeos.tasks.low",
    "batch":    "aeos.tasks.batch",
}

_DEAD_LETTER_TOPIC = "aeos.tasks.dead_letter"

# Event category → topic
_EVENT_TYPE_TOPIC: dict[DistributedEventType, str] = {
    # Cluster
    DistributedEventType.NODE_JOINED:             "aeos.events.cluster",
    DistributedEventType.NODE_LEFT:               "aeos.events.cluster",
    DistributedEventType.NODE_SUSPECTED:          "aeos.events.cluster",
    DistributedEventType.NODE_FAILED:             "aeos.events.cluster",
    DistributedEventType.LEADER_CHANGED:          "aeos.events.cluster",
    # Governance
    DistributedEventType.TOKEN_ISSUED:            "aeos.events.governance",
    DistributedEventType.TOKEN_REVOKED:           "aeos.events.governance",
    DistributedEventType.POLICY_CHANGED:          "aeos.events.governance",
    DistributedEventType.RBAC_REVOKED:            "aeos.events.governance",
    # Capability
    DistributedEventType.CAPABILITY_REGISTERED:   "aeos.events.capability",
    DistributedEventType.CAPABILITY_DEREGISTERED: "aeos.events.capability",
    DistributedEventType.CAPABILITY_DEGRADED:     "aeos.events.capability",
    # Task lifecycle (accepted/completed/failed use execution topic)
    DistributedEventType.TASK_SUBMITTED:  "aeos.events.execution",
    DistributedEventType.TASK_ACCEPTED:   "aeos.events.execution",
    DistributedEventType.TASK_COMPLETED:  "aeos.events.execution",
    DistributedEventType.TASK_FAILED:     "aeos.events.execution",
    DistributedEventType.TASK_CANCELLED:  "aeos.events.execution",
    DistributedEventType.TASK_RETRY:      "aeos.events.execution",
}


class DefaultEventRouter(EventRouter):
    """
    Routes events to the correct Kafka topic.

    TASK_SUBMITTED from a scheduler uses the priority-keyed task topic.
    All other event types route through _EVENT_TYPE_TOPIC.
    """

    def route(self, envelope: EventEnvelope) -> str:
        if envelope.event_type == DistributedEventType.TASK_SUBMITTED:
            priority = envelope.payload.get("priority", "normal")
            # Dead-letter if retry count exceeded (set by scheduler)
            if envelope.payload.get("dead_letter", False):
                return _DEAD_LETTER_TOPIC
            return _TASK_PRIORITY_TOPICS.get(priority, _TASK_PRIORITY_TOPICS["normal"])
        return _EVENT_TYPE_TOPIC.get(envelope.event_type, "aeos.events.cluster")

    def partition_key(self, envelope: EventEnvelope) -> bytes | None:
        if envelope.workflow_id:
            return envelope.workflow_id.encode()
        if envelope.task_id:
            return envelope.task_id.encode()
        return None
