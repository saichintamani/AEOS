"""
Test transport with capture-first delivery and fault injection.

Messages are captured in an internal buffer. Call drain() to deliver
all buffered messages to subscribers. Use set_fail(topic) to simulate
publish errors. Call reset() to clear all state.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict

from app.distributed.contracts.transport import MessageHandler, MessageTransport, TransportMessage


class TestTransport(MessageTransport):
    """
    Deterministic test transport.

    publish() captures messages without delivering them.
    Call drain() to flush captured messages to all subscribers.
    """

    def __init__(self) -> None:
        self._captured: list[TransportMessage] = []
        self._subs: dict[str, dict[str, list[MessageHandler]]] = defaultdict(lambda: defaultdict(list))
        self._sub_map: dict[str, tuple[str, str, MessageHandler]] = {}
        self._fail_topics: set[str] = set()
        self._published: dict[str, int] = defaultdict(int)
        self._running = False

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    async def publish(self, message: TransportMessage, *, wait_for_ack: bool = True) -> None:
        if message.topic in self._fail_topics:
            raise RuntimeError(f"Injected failure on topic {message.topic!r}")
        self._captured.append(message)
        self._published[message.topic] += 1

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
        for topic in topics:
            self._subs[topic][group_id].append(handler)
            self._sub_map[sub_id] = (topic, group_id, handler)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> None:
        entry = self._sub_map.pop(subscription_id, None)
        if entry:
            topic, group_id, handler = entry
            handlers = self._subs.get(topic, {}).get(group_id, [])
            if handler in handlers:
                handlers.remove(handler)

    async def commit(self, subscription_id: str, message: TransportMessage) -> None:
        pass

    async def health_check(self) -> bool:
        return self._running

    async def drain(self) -> None:
        """Deliver all captured messages to subscribers and clear the buffer."""
        to_deliver = list(self._captured)
        self._captured.clear()
        for msg in to_deliver:
            for group_handlers in self._subs.get(msg.topic, {}).values():
                for handler in group_handlers:
                    await handler(msg)

    def set_fail(self, topic: str) -> None:
        self._fail_topics.add(topic)

    def clear_fail(self, topic: str) -> None:
        self._fail_topics.discard(topic)

    def reset(self) -> None:
        self._captured.clear()
        self._subs.clear()
        self._sub_map.clear()
        self._fail_topics.clear()
        self._published.clear()

    def published_count(self, topic: str) -> int:
        return self._published.get(topic, 0)

    @property
    def captured(self) -> list[TransportMessage]:
        return list(self._captured)

    @property
    def is_running(self) -> bool:
        return self._running
