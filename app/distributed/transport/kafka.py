"""
Wave 9B.5.1 — Kafka Transport Adapter

Production MessageTransport backed by Apache Kafka via aiokafka.

Features:
  - AIOKafka async producer / consumer
  - Consumer groups (ADR-002)
  - Retry with exponential back-off
  - Dead-letter queue (DLQ) on max retries
  - Batch compression (lz4)
  - Offset recovery on restart
  - Exactly-documented at-least-once semantics (AC-EXEC-002)

Design decisions:
  - One AIOKafkaProducer shared across all publish() calls.
  - One AIOKafkaConsumer per subscribe() call, each in its own asyncio task.
  - commit() flushes offsets for exactly-processed messages.
  - DLQ topic = original topic + ".dlq"
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.distributed.contracts.transport import MessageHandler, MessageTransport, TransportMessage

logger = logging.getLogger(__name__)

_DEFAULT_BOOTSTRAP = "localhost:9092"
_MAX_RETRIES = 3
_RETRY_BASE_MS = 100
_COMPRESSION = "lz4"


def _require_aiokafka() -> Any:
    try:
        import aiokafka
        return aiokafka
    except ImportError as exc:
        raise ImportError(
            "aiokafka is required for KafkaTransport. "
            "Install it with: pip install aiokafka"
        ) from exc


@dataclass
class _Subscription:
    sub_id: str
    topics: list[str]
    group_id: str
    handler: MessageHandler
    auto_commit: bool
    consumer_task: asyncio.Task | None = None
    consumer: Any = None   # AIOKafkaConsumer


class KafkaTransport(MessageTransport):
    """
    Production Kafka transport adapter.

    Usage::

        transport = KafkaTransport(bootstrap_servers="broker1:9092,broker2:9092")
        await transport.start()
        await transport.subscribe(["aeos.events.cluster"], "aeos-workers", handler)
        await transport.publish(TransportMessage(topic="aeos.events.cluster", payload=b"..."))
        await transport.stop()
    """

    def __init__(
        self,
        bootstrap_servers: str = _DEFAULT_BOOTSTRAP,
        *,
        client_id: str | None = None,
        enable_dlq: bool = True,
        max_retries: int = _MAX_RETRIES,
        compression_type: str = _COMPRESSION,
    ) -> None:
        self._bootstrap = bootstrap_servers
        self._client_id = client_id or f"aeos-{uuid.uuid4().hex[:8]}"
        self._enable_dlq = enable_dlq
        self._max_retries = max_retries
        self._compression = compression_type

        self._producer: Any = None
        self._subscriptions: dict[str, _Subscription] = {}
        self._running = False

    # ── MessageTransport interface ────────────────────────────────────────────

    async def start(self) -> None:
        ak = _require_aiokafka()
        self._producer = ak.AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            compression_type=self._compression,
            enable_idempotence=True,   # exactly-once producer
            acks="all",
        )
        await self._producer.start()
        self._running = True
        logger.info("KafkaTransport: producer started (bootstrap=%s)", self._bootstrap)

    async def stop(self) -> None:
        self._running = False
        # Cancel all consumer tasks
        for sub in list(self._subscriptions.values()):
            await self._stop_subscription(sub)
        if self._producer:
            await self._producer.stop()
            self._producer = None
        logger.info("KafkaTransport: stopped")

    async def publish(
        self,
        message: TransportMessage,
        *,
        wait_for_ack: bool = True,
    ) -> None:
        if not self._running or not self._producer:
            raise RuntimeError("KafkaTransport not started")

        headers = [(k, v.encode()) for k, v in message.headers.items()]
        headers += [
            ("message_id", message.message_id.encode()),
            ("produced_at", message.produced_at.encode()),
        ]
        if message.trace_id:
            headers.append(("trace_id", message.trace_id.encode()))

        for attempt in range(self._max_retries + 1):
            try:
                fut = await self._producer.send(
                    message.topic,
                    value=message.payload,
                    key=message.key,
                    headers=headers,
                    partition=message.partition,
                )
                if wait_for_ack:
                    await fut
                return
            except Exception as exc:
                if attempt >= self._max_retries:
                    logger.error(
                        "KafkaTransport: publish failed after %d retries to %s: %s",
                        self._max_retries, message.topic, exc,
                    )
                    if self._enable_dlq:
                        await self._send_to_dlq(message, str(exc))
                    raise
                delay = _RETRY_BASE_MS * (2 ** attempt) / 1000.0
                logger.warning(
                    "KafkaTransport: publish attempt %d failed, retrying in %.2fs: %s",
                    attempt + 1, delay, exc,
                )
                await asyncio.sleep(delay)

    async def subscribe(
        self,
        topics: list[str],
        group_id: str,
        handler: MessageHandler,
        *,
        auto_commit: bool = False,
    ) -> str:
        ak = _require_aiokafka()
        sub_id = str(uuid.uuid4())

        consumer = ak.AIOKafkaConsumer(
            *topics,
            bootstrap_servers=self._bootstrap,
            group_id=group_id,
            client_id=f"{self._client_id}-consumer-{sub_id[:8]}",
            enable_auto_commit=auto_commit,
            auto_offset_reset="earliest",
        )
        await consumer.start()

        sub = _Subscription(
            sub_id=sub_id,
            topics=list(topics),
            group_id=group_id,
            handler=handler,
            auto_commit=auto_commit,
            consumer=consumer,
        )
        sub.consumer_task = asyncio.create_task(
            self._consume_loop(sub),
            name=f"kafka-consumer-{sub_id[:8]}",
        )
        self._subscriptions[sub_id] = sub
        logger.info(
            "KafkaTransport: subscribed to %s as group '%s' (sub=%s)",
            topics, group_id, sub_id,
        )
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> None:
        sub = self._subscriptions.pop(subscription_id, None)
        if sub:
            await self._stop_subscription(sub)

    async def commit(self, subscription_id: str, message: TransportMessage) -> None:
        sub = self._subscriptions.get(subscription_id)
        if sub and sub.consumer and not sub.auto_commit:
            await sub.consumer.commit()

    async def health_check(self) -> bool:
        if not self._running or not self._producer:
            return False
        try:
            await asyncio.wait_for(self._producer.client._wait_for_metadata_update(), timeout=2.0)
            return True
        except Exception:
            return False

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _consume_loop(self, sub: _Subscription) -> None:
        try:
            async for kafka_msg in sub.consumer:
                if not self._running:
                    break
                headers = {k: v.decode(errors="replace") for k, v in (kafka_msg.headers or [])}
                msg = TransportMessage(
                    topic=kafka_msg.topic,
                    payload=kafka_msg.value or b"",
                    message_id=headers.pop("message_id", str(uuid.uuid4())),
                    produced_at=headers.pop("produced_at", ""),
                    headers=headers,
                    partition=kafka_msg.partition,
                    offset=kafka_msg.offset,
                    key=kafka_msg.key,
                    trace_id=headers.get("trace_id"),
                )
                try:
                    await sub.handler(msg)
                    if not sub.auto_commit:
                        await sub.consumer.commit()
                except Exception:
                    logger.exception(
                        "KafkaTransport: handler error on topic %s offset %s",
                        kafka_msg.topic, kafka_msg.offset,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("KafkaTransport: consumer loop died for sub %s", sub.sub_id)

    async def _stop_subscription(self, sub: _Subscription) -> None:
        if sub.consumer_task and not sub.consumer_task.done():
            sub.consumer_task.cancel()
            try:
                await sub.consumer_task
            except asyncio.CancelledError:
                pass
        if sub.consumer:
            try:
                await sub.consumer.stop()
            except Exception:
                pass

    async def _send_to_dlq(self, message: TransportMessage, error: str) -> None:
        dlq_topic = message.topic + ".dlq"
        dlq_msg = TransportMessage(
            topic=dlq_topic,
            payload=message.payload,
            headers={**message.headers, "dlq_error": error[:256]},
            key=message.key,
        )
        try:
            await self._producer.send(dlq_topic, value=dlq_msg.payload)
            logger.warning("KafkaTransport: message sent to DLQ topic %s", dlq_topic)
        except Exception:
            logger.exception("KafkaTransport: DLQ send failed for topic %s", message.topic)
