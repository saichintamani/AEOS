"""
Unit tests — consumer_group_for_topic (CT-T1-006), JsonEventSerializer,
DefaultEventRouter, DefaultEventPublisher, DefaultEventConsumer.

ADR: ADR-002
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.events import (
    DistributedEventType,
    EventEnvelope,
    TASK_TOPICS,
    SHARED_CONSUMER_GROUP,
    consumer_group_for_topic,
    per_worker_group_id,
)
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.transport.test import TestTransport


class TestConsumerGroupSelection:
    """CT-T1-006: consumer group selection enforces ADR-002."""

    def test_task_topic_uses_shared_group(self):
        for topic in TASK_TOPICS:
            assert consumer_group_for_topic(topic, "w1") == SHARED_CONSUMER_GROUP

    def test_event_topic_uses_per_worker_group(self):
        group = consumer_group_for_topic("aeos.events.cluster", "worker-42")
        assert group == per_worker_group_id("worker-42")
        assert "worker-42" in group

    def test_unknown_topic_defaults_to_per_worker(self):
        group = consumer_group_for_topic("aeos.unknown.thing", "n1")
        assert group == per_worker_group_id("n1")


class TestJsonEventSerializer:

    def test_round_trip(self):
        ser = JsonEventSerializer()
        env = EventEnvelope(
            event_type=DistributedEventType.NODE_JOINED,
            payload={"node_id": "n1"},
            source_node_id="n1",
        )
        data = ser.serialize(env)
        recovered = ser.deserialize(data)
        assert recovered.event_type == DistributedEventType.NODE_JOINED
        assert recovered.payload["node_id"] == "n1"

    def test_unknown_event_type_raises(self):
        import json
        ser = JsonEventSerializer()
        raw = json.dumps({"schema_version": 1, "event_type": "unknown.type",
                          "payload": {}, "source_node_id": "", "event_id": "",
                          "sequence_nanos": 0, "timestamp": "", "headers": {},
                          "correlation_id": None}).encode()
        with pytest.raises(ValueError, match="Unknown event_type"):
            ser.deserialize(raw)

    def test_version_mismatch_raises(self):
        import json
        ser = JsonEventSerializer()
        raw = json.dumps({"schema_version": 99, "event_type": "cluster.node.joined",
                          "payload": {}}).encode()
        with pytest.raises(ValueError, match="schema version"):
            ser.deserialize(raw)


class TestDefaultEventRouter:

    def test_node_joined_routes_to_cluster(self):
        router = DefaultEventRouter()
        env = EventEnvelope(
            event_type=DistributedEventType.NODE_JOINED,
            payload={},
        )
        assert router.route(env) == "aeos.events.cluster"

    def test_task_submitted_routes_to_priority_topic(self):
        router = DefaultEventRouter()
        for priority, expected in [
            ("critical", "aeos.tasks.critical"),
            ("high",     "aeos.tasks.high"),
            ("normal",   "aeos.tasks.normal"),
            ("low",      "aeos.tasks.low"),
            ("batch",    "aeos.tasks.batch"),
        ]:
            env = EventEnvelope(
                event_type=DistributedEventType.TASK_SUBMITTED,
                payload={"priority": priority},
            )
            assert router.route(env) == expected

    def test_dead_letter_routing(self):
        router = DefaultEventRouter()
        env = EventEnvelope(
            event_type=DistributedEventType.TASK_SUBMITTED,
            payload={"priority": "normal", "dead_letter": True},
        )
        assert router.route(env) == "aeos.tasks.dead_letter"

    def test_partition_key_uses_workflow_id(self):
        router = DefaultEventRouter()
        env = EventEnvelope(
            event_type=DistributedEventType.NODE_JOINED,
            payload={},
            workflow_id="wf-abc",
        )
        assert router.partition_key(env) == b"wf-abc"


class TestDefaultEventPublisher:

    @pytest.mark.asyncio
    async def test_publish_stamps_sequence_nanos(self):
        transport = TestTransport()
        await transport.start()
        ser = JsonEventSerializer()
        clock = MonotonicClock()
        router = DefaultEventRouter()
        publisher = DefaultEventPublisher(
            clock=clock, router=router, serializer=ser, transport=transport,
            source_node_id="n1",
        )
        env = EventEnvelope(event_type=DistributedEventType.NODE_JOINED, payload={})
        await publisher.publish(env)
        assert env.sequence_nanos > 0
        assert env.source_node_id == "n1"

    @pytest.mark.asyncio
    async def test_publish_many(self):
        transport = TestTransport()
        await transport.start()
        publisher = DefaultEventPublisher(
            clock=MonotonicClock(),
            router=DefaultEventRouter(),
            serializer=JsonEventSerializer(),
            transport=transport,
        )
        envs = [
            EventEnvelope(event_type=DistributedEventType.NODE_JOINED, payload={})
            for _ in range(3)
        ]
        await publisher.publish_many(envs)
        assert transport.published_count("aeos.events.cluster") == 3
