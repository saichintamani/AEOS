"""
In-memory message transport for testing and local deployments.

Semantics:
  - Competing consumer (round-robin within group_id): multiple subscribers
    on the same topic + group_id receive messages in turn.
  - Fan-out: different group_ids each receive a copy of every message.

Contract: AC-TRANS-001
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict

from app.distributed.contracts.transport import MessageHandler, MessageTransport, TransportMessage

logger = logging.getLogger(__name__)


class InMemoryTransport(MessageTransport):
    """
    In-process message broker.

    subscribe() accepts either a single topic string or a list[str] for
    compatibility. Internally keyed by (topic, group_id).
    """

    def __init__(self) -> None:
        # topic → group_id → [handler, ...]
        self._subs: dict[str, dict[str, list[MessageHandler]]] = defaultdict(lambda: defaultdict(list))
        self._sub_map: dict[str, tuple[str, str, MessageHandler]] = {}  # sub_id → (topic, group_id, handler)
        self._lock = asyncio.Lock()
        self._running = False
        self._published: dict[str, int] = defaultdict(int)
        self._rr_index: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def publish(self, message: TransportMessage, *, wait_for_ack: bool = True) -> None:
        async with self._lock:
            topic_subs = self._subs.get(message.topic, {})
            self._published[message.topic] += 1
            if not topic_subs:
                return
            for group_id, handlers in list(topic_subs.items()):
                if not handlers:
                    continue
                idx = self._rr_index[message.topic][group_id] % len(handlers)
                self._rr_index[message.topic][group_id] = idx + 1
                handler = handlers[idx]
                asyncio.create_task(self._safe_call(handler, message))

    @staticmethod
    async def _safe_call(handler: MessageHandler, msg: TransportMessage) -> None:
        try:
            await handler(msg)
        except Exception:
            logger.exception("InMemoryTransport handler error")

    async def subscribe(
        self,
        topics: list[str] | str,
        group_id: str,
        handler: MessageHandler,
        *,
        auto_commit: bool = False,
    ) -> str:
        if isinstance(topics, str):
            topics = [topics]
        sub_id = str(uuid.uuid4())
        async with self._lock:
            for topic in topics:
                self._subs[topic][group_id].append(handler)
                self._sub_map[sub_id] = (topic, group_id, handler)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            entry = self._sub_map.pop(subscription_id, None)
            if entry:
                topic, group_id, handler = entry
                handlers = self._subs.get(topic, {}).get(group_id, [])
                if handler in handlers:
                    handlers.remove(handler)

    async def commit(self, subscription_id: str, message: TransportMessage) -> None:
        pass  # no-op for in-memory; offsets are automatic

    async def health_check(self) -> bool:
        return self._running

    def published_count(self, topic: str) -> int:
        return self._published.get(topic, 0)

    @property
    def is_running(self) -> bool:
        return self._running
