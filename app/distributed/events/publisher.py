"""
Default event publisher.

Composes DistributedClock + EventRouter + EventSerializer + MessageTransport
to publish EventEnvelopes to the correct Kafka topic with full metadata.

Contract: AC-IFACE-003, AC-OBS-003
"""

from __future__ import annotations

from app.distributed.contracts.coordination import DistributedClock
from app.distributed.contracts.events import EventEnvelope, EventPublisher, EventRouter, EventSerializer
from app.distributed.contracts.transport import MessageTransport, TransportMessage


class DefaultEventPublisher(EventPublisher):
    """
    Stamps sequence_nanos from the distributed clock, routes, serializes,
    and delivers via transport.
    """

    def __init__(
        self,
        clock: DistributedClock,
        router: EventRouter,
        serializer: EventSerializer,
        transport: MessageTransport,
        *,
        source_node_id: str = "",
        source_service: str = "",
    ) -> None:
        self._clock = clock
        self._router = router
        self._serializer = serializer
        self._transport = transport
        self._source_node_id = source_node_id
        self._source_service = source_service

    async def publish(
        self,
        envelope: EventEnvelope,
        *,
        wait_for_ack: bool = True,
    ) -> None:
        envelope.sequence_nanos = self._clock.now_nanos()
        if not envelope.source_node_id:
            envelope.source_node_id = self._source_node_id
        if not envelope.source_service:
            envelope.source_service = self._source_service

        topic = self._router.route(envelope)
        key = self._router.partition_key(envelope)
        data = self._serializer.serialize(envelope)
        headers = envelope.to_headers()

        msg = TransportMessage(
            topic=topic,
            key=key,
            payload=data,
            headers=headers,
        )
        await self._transport.publish(msg)

    async def publish_many(self, envelopes: list[EventEnvelope]) -> None:
        for envelope in envelopes:
            await self.publish(envelope)

    # Convenience: publish to an explicit topic (bypasses router)
    async def publish_to(
        self,
        topic: str,
        envelope: EventEnvelope,
    ) -> None:
        envelope.sequence_nanos = self._clock.now_nanos()
        if not envelope.source_node_id:
            envelope.source_node_id = self._source_node_id
        data = self._serializer.serialize(envelope)
        msg = TransportMessage(
            topic=topic,
            key=self._router.partition_key(envelope),
            payload=data,
            headers=envelope.to_headers(),
        )
        await self._transport.publish(msg)
