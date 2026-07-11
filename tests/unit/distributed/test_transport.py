"""
Unit tests — InMemoryTransport and TestTransport.
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.transport import TransportMessage
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.transport.test import TestTransport


def _msg(topic: str, data: bytes = b"hello") -> TransportMessage:
    return TransportMessage(topic=topic, payload=data)


class TestInMemoryTransport:

    @pytest.mark.asyncio
    async def test_publish_and_receive(self):
        t = InMemoryTransport()
        await t.start()
        received = []

        async def handler(msg: TransportMessage):
            received.append(msg)

        await t.subscribe("my-topic", "grp", handler)
        await t.publish(_msg("my-topic"))
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].payload == b"hello"

    @pytest.mark.asyncio
    async def test_competing_consumer_round_robin(self):
        """Two handlers in the same group should share messages (round-robin)."""
        t = InMemoryTransport()
        await t.start()
        counts = [0, 0]

        async def h1(msg): counts[0] += 1
        async def h2(msg): counts[1] += 1

        await t.subscribe("t", "g", h1)
        await t.subscribe("t", "g", h2)

        for _ in range(4):
            await t.publish(_msg("t"))
        await asyncio.sleep(0.05)
        assert counts[0] + counts[1] == 4
        assert counts[0] == 2
        assert counts[1] == 2

    @pytest.mark.asyncio
    async def test_fan_out_across_groups(self):
        """Different group_ids each receive every message."""
        t = InMemoryTransport()
        await t.start()
        a, b = [], []

        async def ha(msg): a.append(msg)
        async def hb(msg): b.append(msg)

        await t.subscribe("t", "g1", ha)
        await t.subscribe("t", "g2", hb)

        await t.publish(_msg("t"))
        await asyncio.sleep(0.05)
        assert len(a) == 1
        assert len(b) == 1

    @pytest.mark.asyncio
    async def test_published_count(self):
        t = InMemoryTransport()
        await t.start()
        await t.publish(_msg("t"))
        await t.publish(_msg("t"))
        assert t.published_count("t") == 2
        assert t.published_count("other") == 0

    @pytest.mark.asyncio
    async def test_health_check(self):
        t = InMemoryTransport()
        assert not await t.health_check()
        await t.start()
        assert await t.health_check()


class TestTestTransport:

    @pytest.mark.asyncio
    async def test_capture_without_delivery(self):
        t = TestTransport()
        await t.start()
        received = []

        async def handler(msg): received.append(msg)
        await t.subscribe("t", "g", handler)

        await t.publish(_msg("t"))
        # Not delivered yet
        assert received == []
        assert len(t.captured) == 1

    @pytest.mark.asyncio
    async def test_drain_delivers(self):
        t = TestTransport()
        await t.start()
        received = []

        async def handler(msg): received.append(msg)
        await t.subscribe("t", "g", handler)
        await t.publish(_msg("t"))
        await t.drain()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_set_fail_raises(self):
        t = TestTransport()
        await t.start()
        t.set_fail("bad-topic")
        with pytest.raises(RuntimeError):
            await t.publish(_msg("bad-topic"))

    @pytest.mark.asyncio
    async def test_reset_clears_everything(self):
        t = TestTransport()
        await t.start()
        t.set_fail("t")
        await t.publish(_msg("t2"))
        t.reset()
        assert t.captured == []
        # Should not raise after reset
        await t.publish(_msg("t"))
