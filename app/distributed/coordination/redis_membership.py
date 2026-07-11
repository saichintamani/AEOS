"""
Wave 9B.5.5 — Redis-backed Cluster Membership Store

Replaces InMemoryMembershipStore for production deployments.

Each member is stored as a Redis Hash under key `aeos:members:{node_id}`.
An index set `aeos:members:index` tracks all known node_ids.

MemberRecord serialisation: JSON field-by-field.
TTL on each member hash = heartbeat_interval * 3 (auto-expire dead nodes).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.distributed.contracts.cluster import ClusterMemberState, MemberRecord
from app.distributed.cluster.membership import MembershipStore

logger = logging.getLogger(__name__)

_KEY_PREFIX = "aeos:members:"
_INDEX_KEY = "aeos:members:index"
_DEFAULT_TTL = 90   # seconds


def _require_redis() -> Any:
    try:
        import redis.asyncio as aioredis
        return aioredis
    except ImportError as exc:
        raise ImportError(
            "redis[asyncio] is required for RedisMembershipStore. "
            "Install it with: pip install 'redis[asyncio]'"
        ) from exc


def _serialize(record: MemberRecord) -> str:
    return json.dumps({
        "node_id": record.node_id,
        "address": record.address,
        "port": record.port,
        "role": record.role,
        "state": record.state.value,
        "joined_at": record.joined_at,
        "last_heartbeat": record.last_heartbeat,
        "metadata": record.metadata,
        "missed_heartbeats": record.missed_heartbeats,
    })


def _deserialize(raw: str | bytes) -> MemberRecord:
    data = json.loads(raw)
    return MemberRecord(
        node_id=data["node_id"],
        address=data["address"],
        port=data["port"],
        role=data.get("role", "worker"),
        state=ClusterMemberState(data["state"]),
        joined_at=data.get("joined_at", ""),
        last_heartbeat=data.get("last_heartbeat", ""),
        metadata=data.get("metadata", {}),
        missed_heartbeats=data.get("missed_heartbeats", 0),
    )


class RedisMembershipStore(MembershipStore):
    """
    Redis-backed MembershipStore.

    Each record lives as a String key at `aeos:members:{node_id}` with a TTL.
    An index set at `aeos:members:index` allows listing all members.
    """

    def __init__(self, url: str = "redis://localhost:6379/0", ttl: int = _DEFAULT_TTL) -> None:
        self._url = url
        self._ttl = ttl
        self._redis: Any = None

    async def connect(self) -> None:
        aioredis = _require_redis()
        self._redis = aioredis.from_url(self._url, decode_responses=False)
        logger.info("RedisMembershipStore: connected to %s", self._url)

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def add(self, record: MemberRecord) -> None:
        self._ensure_connected()
        key = (_KEY_PREFIX + record.node_id).encode()
        await self._redis.set(key, _serialize(record).encode(), ex=self._ttl)
        await self._redis.sadd(_INDEX_KEY.encode(), record.node_id.encode())

    async def update(self, record: MemberRecord) -> None:
        await self.add(record)   # SET is idempotent; resets TTL

    async def get(self, node_id: str) -> MemberRecord | None:
        self._ensure_connected()
        raw = await self._redis.get((_KEY_PREFIX + node_id).encode())
        if not raw:
            return None
        try:
            return _deserialize(raw)
        except Exception:
            logger.warning("RedisMembershipStore: corrupt record for %s", node_id)
            return None

    async def remove(self, node_id: str) -> None:
        self._ensure_connected()
        await self._redis.delete((_KEY_PREFIX + node_id).encode())
        await self._redis.srem(_INDEX_KEY.encode(), node_id.encode())

    async def all(self) -> list[MemberRecord]:
        self._ensure_connected()
        node_ids_raw = await self._redis.smembers(_INDEX_KEY.encode())
        records = []
        for nid_raw in node_ids_raw:
            nid = nid_raw.decode() if isinstance(nid_raw, bytes) else nid_raw
            record = await self.get(nid)
            if record:
                records.append(record)
            else:
                # Node expired — clean from index
                await self._redis.srem(_INDEX_KEY.encode(), nid_raw)
        return records

    async def by_state(self, state: ClusterMemberState) -> list[MemberRecord]:
        all_records = await self.all()
        return [r for r in all_records if r.state == state]

    async def refresh_ttl(self, node_id: str) -> None:
        """Reset TTL for a node on heartbeat — prevents auto-expiry of live nodes."""
        self._ensure_connected()
        await self._redis.expire((_KEY_PREFIX + node_id).encode(), self._ttl)

    def _ensure_connected(self) -> None:
        if not self._redis:
            raise RuntimeError("RedisMembershipStore not connected — call connect() first")
