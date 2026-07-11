"""
Unit tests — MonotonicClock monotonicity, InMemoryLeaseStore (PROTO-019),
InMemoryClusterMetadata.
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.coordination.metadata import InMemoryClusterMetadata
from app.distributed.cluster.exceptions import LeaseNotHeld


class TestMonotonicClock:

    def test_monotonically_increasing(self):
        clock = MonotonicClock()
        samples = [clock.now_nanos() for _ in range(1000)]
        for a, b in zip(samples, samples[1:]):
            assert b > a, "Clock must be strictly monotonic"

    def test_now_iso_returns_string(self):
        clock = MonotonicClock()
        ts = clock.now_iso()
        assert isinstance(ts, str)
        assert "T" in ts  # ISO 8601

    def test_resolution_ns(self):
        clock = MonotonicClock()
        assert clock.resolution_ns == 1


class TestInMemoryLeaseStore:

    @pytest.mark.asyncio
    async def test_acquire_succeeds_on_empty_key(self):
        store = InMemoryLeaseStore()
        record = await store.acquire("k", "holder-1", 60)
        assert record is not None
        assert record.holder_id == "holder-1"

    @pytest.mark.asyncio
    async def test_acquire_fails_on_contention(self):
        store = InMemoryLeaseStore()
        r1 = await store.acquire("k", "holder-1", 60)
        r2 = await store.acquire("k", "holder-2", 60)
        assert r1 is not None
        assert r2 is None  # contention

    @pytest.mark.asyncio
    async def test_release_and_reacquire(self):
        store = InMemoryLeaseStore()
        await store.acquire("k", "h1", 60)
        await store.release("k", "h1")
        r = await store.acquire("k", "h2", 60)
        assert r is not None
        assert r.holder_id == "h2"

    @pytest.mark.asyncio
    async def test_release_wrong_holder_raises(self):
        store = InMemoryLeaseStore()
        await store.acquire("k", "h1", 60)
        with pytest.raises(LeaseNotHeld):
            await store.release("k", "h2")

    @pytest.mark.asyncio
    async def test_get_returns_record(self):
        store = InMemoryLeaseStore()
        await store.acquire("k", "h1", 60)
        rec = await store.get("k")
        assert rec is not None
        assert rec.holder_id == "h1"

    @pytest.mark.asyncio
    async def test_is_held_by(self):
        store = InMemoryLeaseStore()
        await store.acquire("k", "h1", 60)
        assert await store.is_held_by("k", "h1")
        assert not await store.is_held_by("k", "h2")

    @pytest.mark.asyncio
    async def test_concurrent_acquire_only_one_wins(self):
        store = InMemoryLeaseStore()
        results = await asyncio.gather(
            store.acquire("k", "w1", 60),
            store.acquire("k", "w2", 60),
            store.acquire("k", "w3", 60),
        )
        winners = [r for r in results if r is not None]
        assert len(winners) == 1


class TestInMemoryClusterMetadata:

    @pytest.mark.asyncio
    async def test_default_topics_present(self):
        meta = InMemoryClusterMetadata()
        topics = await meta.list_topics()
        assert "aeos.events.cluster" in topics
        assert "aeos.events.execution" in topics
        assert len(topics) == 9

    @pytest.mark.asyncio
    async def test_get_partition_count(self):
        meta = InMemoryClusterMetadata()
        assert await meta.get_partition_count("aeos.events.task") == 24

    @pytest.mark.asyncio
    async def test_unknown_topic_raises(self):
        meta = InMemoryClusterMetadata()
        with pytest.raises(KeyError):
            await meta.get_partition_count("unknown.topic")

    @pytest.mark.asyncio
    async def test_set_and_get_setting(self):
        meta = InMemoryClusterMetadata()
        await meta.set_setting("max_lag", 1000)
        val = await meta.get_setting("max_lag")
        assert val == 1000

    @pytest.mark.asyncio
    async def test_get_leader_default_none(self):
        meta = InMemoryClusterMetadata()
        assert await meta.get_leader_node_id() is None

    @pytest.mark.asyncio
    async def test_set_leader(self):
        meta = InMemoryClusterMetadata()
        await meta.set_leader("node-1")
        assert await meta.get_leader_node_id() == "node-1"
