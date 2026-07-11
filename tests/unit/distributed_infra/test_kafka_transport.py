"""
Unit tests for Wave 9B.5.1 — KafkaTransport.

All aiokafka dependencies are mocked — no real Kafka required.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.distributed.contracts.transport import TransportMessage


# ── helpers ──────────────────────────────────────────────────────────────────

def _msg(topic: str = "aeos.events.cluster", payload: bytes = b'{"k":1}') -> TransportMessage:
    return TransportMessage(topic=topic, payload=payload, trace_id="trace-001")


def _mock_producer():
    p = AsyncMock()
    p.start = AsyncMock()
    p.stop = AsyncMock()
    # send() returns a future that resolves when awaited
    future = asyncio.Future()
    future.set_result(None)
    p.send = AsyncMock(return_value=future)
    return p


def _mock_consumer():
    c = AsyncMock()
    c.start = AsyncMock()
    c.stop = AsyncMock()
    c.commit = AsyncMock()
    c.__aiter__ = MagicMock(return_value=iter([]))
    return c


# ── publish ───────────────────────────────────────────────────────────────────

class TestKafkaPublish:

    @pytest.mark.asyncio
    async def test_publish_calls_producer_send(self):
        from app.distributed.transport.kafka import KafkaTransport

        producer = _mock_producer()
        with patch("app.distributed.transport.kafka._require_aiokafka") as mock_ak:
            mock_ak.return_value.AIOKafkaProducer.return_value = producer
            transport = KafkaTransport.__new__(KafkaTransport)
            transport._bootstrap = "localhost:9092"
            transport._client_id = "test"
            transport._enable_dlq = True
            transport._max_retries = 3
            transport._compression = "lz4"
            transport._producer = producer
            transport._subscriptions = {}
            transport._running = True

            msg = _msg()
            await transport.publish(msg, wait_for_ack=False)
            assert producer.send.called
            called_topic = producer.send.call_args[0][0]
            assert called_topic == "aeos.events.cluster"

    @pytest.mark.asyncio
    async def test_publish_includes_trace_id_in_headers(self):
        from app.distributed.transport.kafka import KafkaTransport

        producer = _mock_producer()

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 0
        transport._compression = "lz4"
        transport._producer = producer
        transport._subscriptions = {}
        transport._running = True

        msg = _msg()
        msg.trace_id = "trace-xyz"
        await transport.publish(msg, wait_for_ack=False)

        headers = producer.send.call_args[1].get("headers", [])
        header_keys = [h[0] for h in headers]
        assert "trace_id" in header_keys

    @pytest.mark.asyncio
    async def test_publish_not_running_raises(self):
        from app.distributed.transport.kafka import KafkaTransport

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._running = False
        transport._producer = None
        transport._subscriptions = {}
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 0
        transport._compression = "lz4"

        with pytest.raises(RuntimeError):
            await transport.publish(_msg())

    @pytest.mark.asyncio
    async def test_publish_retries_on_transient_error(self):
        from app.distributed.transport.kafka import KafkaTransport

        producer = _mock_producer()
        attempts = []

        success_future = asyncio.Future()
        success_future.set_result(None)

        async def _send(*a, **kw):
            attempts.append(1)
            if len(attempts) < 2:
                raise Exception("transient broker error")
            return success_future

        producer.send = _send

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 3
        transport._compression = "lz4"
        transport._producer = producer
        transport._subscriptions = {}
        transport._running = True

        with patch("asyncio.sleep", new=AsyncMock()):
            await transport.publish(_msg(), wait_for_ack=False)

        assert len(attempts) == 2

    @pytest.mark.asyncio
    async def test_publish_exhausted_retries_raises(self):
        from app.distributed.transport.kafka import KafkaTransport

        producer = _mock_producer()
        producer.send = AsyncMock(side_effect=Exception("permanent failure"))

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 2
        transport._compression = "lz4"
        transport._producer = producer
        transport._subscriptions = {}
        transport._running = True

        with patch("asyncio.sleep", new=AsyncMock()):
            with pytest.raises(Exception, match="permanent failure"):
                await transport.publish(_msg(), wait_for_ack=False)


# ── subscribe ─────────────────────────────────────────────────────────────────

class TestKafkaSubscribe:

    @pytest.mark.asyncio
    async def test_subscribe_returns_subscription_id(self):
        from app.distributed.transport.kafka import KafkaTransport

        consumer = _mock_consumer()

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 3
        transport._compression = "lz4"
        transport._producer = None
        transport._subscriptions = {}
        transport._running = True

        with patch("app.distributed.transport.kafka._require_aiokafka") as mock_ak:
            mock_ak.return_value.AIOKafkaConsumer.return_value = consumer
            sub_id = await transport.subscribe(
                ["aeos.events"], "grp-1", AsyncMock()
            )
        assert isinstance(sub_id, str) and len(sub_id) > 0
        assert sub_id in transport._subscriptions

    @pytest.mark.asyncio
    async def test_subscribe_creates_consumer_task(self):
        from app.distributed.transport.kafka import KafkaTransport

        consumer = _mock_consumer()

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 3
        transport._compression = "lz4"
        transport._producer = None
        transport._subscriptions = {}
        transport._running = True

        with patch("app.distributed.transport.kafka._require_aiokafka") as mock_ak:
            mock_ak.return_value.AIOKafkaConsumer.return_value = consumer
            sub_id = await transport.subscribe(
                ["aeos.events"], "grp-1", AsyncMock()
            )

        sub = transport._subscriptions[sub_id]
        assert sub.consumer_task is not None
        sub.consumer_task.cancel()
        try:
            await sub.consumer_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_unsubscribe_removes_subscription(self):
        from app.distributed.transport.kafka import KafkaTransport

        consumer = _mock_consumer()

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 3
        transport._compression = "lz4"
        transport._producer = None
        transport._subscriptions = {}
        transport._running = True

        with patch("app.distributed.transport.kafka._require_aiokafka") as mock_ak:
            mock_ak.return_value.AIOKafkaConsumer.return_value = consumer
            sub_id = await transport.subscribe(
                ["aeos.events"], "grp-1", AsyncMock()
            )

        await transport.unsubscribe(sub_id)
        assert sub_id not in transport._subscriptions


# ── stop ─────────────────────────────────────────────────────────────────────

class TestKafkaStop:

    @pytest.mark.asyncio
    async def test_stop_calls_producer_stop(self):
        from app.distributed.transport.kafka import KafkaTransport

        producer = _mock_producer()

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._bootstrap = "localhost:9092"
        transport._client_id = "test"
        transport._enable_dlq = False
        transport._max_retries = 3
        transport._compression = "lz4"
        transport._producer = producer
        transport._subscriptions = {}
        transport._running = True

        await transport.stop()
        producer.stop.assert_called_once()
        assert not transport._running

    @pytest.mark.asyncio
    async def test_is_running_property(self):
        from app.distributed.transport.kafka import KafkaTransport

        transport = KafkaTransport.__new__(KafkaTransport)
        transport._running = True
        assert transport.is_running is True
        transport._running = False
        assert transport.is_running is False
