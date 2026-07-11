"""
In-memory membership store implementation.

Thread-safe via asyncio.Lock. Suitable for single-process test/local deployments.
"""

from __future__ import annotations

import asyncio

from app.distributed.contracts.cluster import (
    ClusterMemberState,
    MemberRecord,
    MembershipStore,
    NodeIdentity,
)


class InMemoryMembershipStore(MembershipStore):
    """Dict-backed membership store for testing and local execution."""

    def __init__(self) -> None:
        self._members: dict[str, MemberRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, record: MemberRecord) -> None:
        async with self._lock:
            self._members[record.node_id] = record

    async def update(self, record: MemberRecord) -> None:
        async with self._lock:
            if record.node_id not in self._members:
                raise KeyError(f"Node {record.node_id!r} not found")
            self._members[record.node_id] = record

    async def get(self, node_id: str) -> MemberRecord | None:
        async with self._lock:
            return self._members.get(node_id)

    async def all(self) -> list[MemberRecord]:
        async with self._lock:
            return list(self._members.values())

    async def remove(self, node_id: str) -> None:
        async with self._lock:
            self._members.pop(node_id, None)

    async def by_state(self, state: ClusterMemberState) -> list[MemberRecord]:
        async with self._lock:
            return [m for m in self._members.values() if m.state == state]

    async def count(self) -> int:
        async with self._lock:
            return len(self._members)
