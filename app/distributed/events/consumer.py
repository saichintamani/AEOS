"""
Default event consumer.

Enforces ADR-002 consumer group selection:
  - Task topics (aeos.tasks.*) → SHARED_CONSUMER_GROUP = "aeos-workers"
  - Event topics (aeos.events.*) → per_worker_group_id(node_id)

Fan-out: multiple handlers can subscribe to the same topic;
all receive each message.

Contract: AC-IFACE-003
ADR: ADR-002
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any

from app.distributed.contracts.events import (
    DistributedEventType,
    EventConsumer,
    EventEnvelope,
    EventHandler,
    EventSerializer,
    TASK_TOPICS,
    SHARED_CONSUMER_GROUP,
    per_worker_group_id,
)
from app.distributed.contracts.transport import MessageTransport, TransportMessage
from app.distributed.events.router import _EVENT_TYPE_TOPIC

logger = logging.getLogger(__name__)


def _build_type_to_topic_map() -> dict[DistributedEventType, str]:
    return dict(_EVENT_TYPE_TOPIC)


class DefaultEventConsumer(EventConsumer):
    """
    Subscribes to event types and dispatches received messages to handlers.
    """

    def __init__(
        self,
        transport: MessageTransport,
        serializer: EventSerializer,
        *,
        node_id: str = "",
    ) -> None:
        self._transport = transport
        self._serializer = serializer
        self._node_id = node_id
        self._type_to_topic = _build_type_to_topic_map()
        # topic → list of (subscription_id, handler)
        self._handlers: dict[str, list[tuple[str, EventHandler]]] = defaultdict(list)
        self._sub_to_topic: dict[str, str] = {}
        self._transport_subs: dict[str, str] = {}  # topic → transport sub_id
        self._running = False

    def _group_for_topic(self, topic: str) -> str:
        if topic in TASK_TOPICS:
            return SHARED_CONSUMER_GROUP
        return per_worker_group_id(self._node_id)

    async def subscribe(
        self,
        event_types: list[DistributedEventType],
        handler: EventHandler,
        node_id: str = "",
    ) -> str:
        sub_id = str(uuid.uuid4())
        for et in event_types:
            topic = self._type_to_topic.get(et, "aeos.events.cluster")
            self._handlers[topic].append((sub_id, handler))
            self._sub_to_topic[sub_id] = topic
            if self._running and topic not in self._transport_subs:
                await self._subscribe_topic(topic)
        return sub_id

    async def _subscribe_topic(self, topic: str) -> None:
        if topic in self._transport_subs:
            return
        group = self._group_for_topic(topic)
        cb = self._make_callback(topic)
        t_sub_id = await self._transport.subscribe(topic, group, cb)
        self._transport_subs[topic] = t_sub_id

    def _make_callback(self, topic: str):
        async def _cb(msg: TransportMessage):
            try:
                envelope = self._serializer.deserialize(msg.payload)
            except Exception:
                logger.exception("Failed to deserialize message on %s", topic)
                return
            for _, handler in list(self._handlers.get(topic, [])):
                try:
                    await handler(envelope)
                except Exception:
                    logger.exception("Handler error on topic %s", topic)
        return _cb

    async def start(self) -> None:
        self._running = True
        for topic in list(self._handlers.keys()):
            await self._subscribe_topic(topic)

    async def stop(self) -> None:
        self._running = False

    async def unsubscribe(self, subscription_id: str) -> None:
        topic = self._sub_to_topic.pop(subscription_id, None)
        if topic:
            self._handlers[topic] = [
                (sid, h) for sid, h in self._handlers[topic] if sid != subscription_id
            ]

    # Convenience: directly register a handler for an event type (used by WorkerRuntime)
    def on(self, event_type: DistributedEventType, handler: EventHandler) -> None:
        topic = self._type_to_topic.get(event_type, "aeos.events.cluster")
        sub_id = str(uuid.uuid4())
        self._handlers[topic].append((sub_id, handler))
        self._sub_to_topic[sub_id] = topic
