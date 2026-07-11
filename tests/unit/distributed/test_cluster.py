"""
Unit tests — ClusterMemberState SM, MemberRecord, InMemoryMembershipStore, ClusterMemberManager.

Protocol: PROTO-001, PROTO-002, PROTO-003
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.cluster import ClusterMemberState, MemberRecord, NodeIdentity
from app.distributed.cluster.exceptions import DuplicateNodeError, NodeNotFound
from app.distributed.cluster.membership import InMemoryMembershipStore
from app.distributed.cluster.manager import ClusterMemberManager


def _id(n="n1") -> NodeIdentity:
    return NodeIdentity(node_id=n, host="127.0.0.1", port=9000)


def _rec(n="n1", state=ClusterMemberState.RUNNING) -> MemberRecord:
    return MemberRecord(identity=_id(n), state=state)


class TestInMemoryMembershipStore:

    @pytest.mark.asyncio
    async def test_add_and_get(self):
        store = InMemoryMembershipStore()
        rec = _rec("a")
        await store.add(rec)
        got = await store.get("a")
        assert got is not None
        assert got.state == ClusterMemberState.RUNNING

    @pytest.mark.asyncio
    async def test_update(self):
        store = InMemoryMembershipStore()
        rec = _rec("a")
        await store.add(rec)
        rec.state = ClusterMemberState.SUSPECTED
        await store.update(rec)
        got = await store.get("a")
        assert got.state == ClusterMemberState.SUSPECTED

    @pytest.mark.asyncio
    async def test_remove(self):
        store = InMemoryMembershipStore()
        await store.add(_rec("a"))
        await store.remove("a")
        assert await store.get("a") is None

    @pytest.mark.asyncio
    async def test_all(self):
        store = InMemoryMembershipStore()
        await store.add(_rec("a"))
        await store.add(_rec("b"))
        all_members = await store.all()
        assert len(all_members) == 2

    @pytest.mark.asyncio
    async def test_by_state(self):
        store = InMemoryMembershipStore()
        await store.add(_rec("a", ClusterMemberState.RUNNING))
        await store.add(_rec("b", ClusterMemberState.FAILED))
        running = await store.by_state(ClusterMemberState.RUNNING)
        assert len(running) == 1
        assert running[0].node_id == "a"

    @pytest.mark.asyncio
    async def test_count(self):
        store = InMemoryMembershipStore()
        await store.add(_rec("a"))
        await store.add(_rec("b"))
        assert await store.count() == 2


class TestClusterMemberManager:

    @pytest.mark.asyncio
    async def test_join_registers_running(self):
        mgr = ClusterMemberManager(InMemoryMembershipStore())
        rec = await mgr.join(_id("n1"))
        assert rec.state == ClusterMemberState.RUNNING

    @pytest.mark.asyncio
    async def test_duplicate_join_raises(self):
        mgr = ClusterMemberManager(InMemoryMembershipStore())
        await mgr.join(_id("n1"))
        with pytest.raises(DuplicateNodeError):
            await mgr.join(_id("n1"))

    @pytest.mark.asyncio
    async def test_leave_removes_node(self):
        store = InMemoryMembershipStore()
        mgr = ClusterMemberManager(store)
        await mgr.join(_id("n1"))
        await mgr.leave("n1")
        assert await store.get("n1") is None

    @pytest.mark.asyncio
    async def test_leave_unknown_raises(self):
        mgr = ClusterMemberManager(InMemoryMembershipStore())
        with pytest.raises(NodeNotFound):
            await mgr.leave("ghost")

    @pytest.mark.asyncio
    async def test_heartbeat_resets_missed_count(self):
        store = InMemoryMembershipStore()
        mgr = ClusterMemberManager(store)
        await mgr.join(_id("n1"))
        rec = await store.get("n1")
        rec.missed_heartbeats = 3
        rec.state = ClusterMemberState.SUSPECTED
        await store.update(rec)
        await mgr.record_heartbeat("n1")
        updated = await store.get("n1")
        assert updated.missed_heartbeats == 0
        assert updated.state == ClusterMemberState.RUNNING

    @pytest.mark.asyncio
    async def test_get_active_members(self):
        mgr = ClusterMemberManager(InMemoryMembershipStore())
        await mgr.join(_id("n1"))
        await mgr.join(_id("n2"))
        members = await mgr.get_active_members()
        assert len(members) == 2
