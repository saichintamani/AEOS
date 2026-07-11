"""
In-memory lease store with lazy TTL eviction.

Semantics follow PROTO-019 (SETNX-style acquire).
TTL is enforced via monotonic_ns() comparisons on every read/write.

Contract: AC-CONS-001
ADR: ADR-009
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app.distributed.contracts.coordination import LeaseRecord, LeaseStore
from app.distributed.cluster.exceptions import LeaseNotHeld


class _Entry:
    __slots__ = ("record", "expires_at_ns")

    def __init__(self, record: LeaseRecord, ttl_seconds: int) -> None:
        self.record = record
        self.expires_at_ns = time.monotonic_ns() + ttl_seconds * 1_000_000_000


class InMemoryLeaseStore(LeaseStore):
    """
    Dict-backed lease store for single-process testing and local deployments.

    Expiry is checked lazily on every operation — no background sweep required.
    """

    def __init__(self) -> None:
        self._store: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()

    def _is_expired(self, entry: _Entry) -> bool:
        return time.monotonic_ns() > entry.expires_at_ns

    async def acquire(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
        *,
        metadata: dict[str, str] | None = None,
    ) -> LeaseRecord | None:
        async with self._lock:
            existing = self._store.get(lease_key)
            if existing and not self._is_expired(existing):
                return None  # held by another — contention
            record = LeaseRecord(
                lease_key=lease_key,
                holder_id=holder_id,
                ttl_seconds=ttl_seconds,
                metadata=metadata or {},
            )
            self._store[lease_key] = _Entry(record, ttl_seconds)
            return record

    async def renew(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
    ) -> bool:
        async with self._lock:
            entry = self._store.get(lease_key)
            if not entry or self._is_expired(entry):
                return False
            if entry.record.holder_id != holder_id:
                raise LeaseNotHeld(lease_key, holder_id, entry.record.holder_id)
            entry.expires_at_ns = time.monotonic_ns() + ttl_seconds * 1_000_000_000
            return True

    async def release(
        self,
        lease_key: str,
        holder_id: str,
    ) -> bool:
        async with self._lock:
            entry = self._store.get(lease_key)
            if not entry or self._is_expired(entry):
                return False  # already expired — no-op per PROTO-019
            if entry.record.holder_id != holder_id:
                raise LeaseNotHeld(lease_key, holder_id, entry.record.holder_id)
            del self._store[lease_key]
            return True

    async def get(self, lease_key: str) -> LeaseRecord | None:
        async with self._lock:
            entry = self._store.get(lease_key)
            if not entry or self._is_expired(entry):
                return None
            return entry.record

    async def is_held_by(self, lease_key: str, holder_id: str) -> bool:
        record = await self.get(lease_key)
        return record is not None and record.holder_id == holder_id
