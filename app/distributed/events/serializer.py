"""
JSON event serializer with schema version enforcement.

Schema version 1: envelope fields serialised to a flat JSON dict.
Raises ValueError on version mismatch or unknown event_type.

Contract: AC-COMM-001
"""

from __future__ import annotations

import json

from app.distributed.contracts.events import DistributedEventType, EventEnvelope


_SCHEMA_VERSION = 1


class JsonEventSerializer:
    """Serialize/deserialize EventEnvelope to/from JSON bytes."""

    def serialize(self, envelope: EventEnvelope) -> bytes:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "message_id": envelope.message_id,
            "event_type": envelope.event_type.value,
            "source_node_id": envelope.source_node_id,
            "source_service": envelope.source_service,
            "sequence_nanos": envelope.sequence_nanos,
            "produced_at": envelope.produced_at,
            "payload": envelope.payload,
            "trace_id": envelope.trace_id,
            "span_id": envelope.span_id,
            "workflow_id": envelope.workflow_id,
            "task_id": envelope.task_id,
        }
        return json.dumps(payload, default=str).encode()

    def deserialize(self, data: bytes) -> EventEnvelope:
        raw = json.loads(data)
        version = raw.get("schema_version")
        if version != _SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported schema version: {version!r} (expected {_SCHEMA_VERSION})"
            )
        event_type_str = raw.get("event_type")
        try:
            event_type = DistributedEventType(event_type_str)
        except ValueError:
            raise ValueError(f"Unknown event_type: {event_type_str!r}")

        return EventEnvelope(
            event_type=event_type,
            payload=raw.get("payload", {}),
            source_node_id=raw.get("source_node_id", ""),
            source_service=raw.get("source_service", ""),
            message_id=raw.get("message_id", ""),
            sequence_nanos=raw.get("sequence_nanos", 0),
            produced_at=raw.get("produced_at", ""),
            trace_id=raw.get("trace_id"),
            span_id=raw.get("span_id"),
            workflow_id=raw.get("workflow_id"),
            task_id=raw.get("task_id"),
        )
