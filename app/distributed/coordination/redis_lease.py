"""
Wave 9B.5.2 — Redis Lease Store

Production LeaseStore backed by Redis via redis.asyncio.

Protocol: PROTO-019 (SETNX lease acquisition / renewal / release)
Contract: AC-CONS-001

Design:
  - acquire():  SET NX EX — atomic, no Lua script needed for simple acquire
  - renew():    Lua script — check holder then EXPIRE atomically
  - release():  Lua script — check holder then DEL atomically
  - Lease value = JSON {holder_id, acquired_at, metadata}
  - Clock drift: TTL padded by CLOCK_DRIFT_ALLOWANCE_S to tolerate skew
  - Split-brain: fencing tokens (monotonically increasing int stored alongside)

DRIFT-001 fix (P12A.1):
  - Uses create_redis_client() factory which supports RedisCluster mode
  - All lease keys use {aeos:lease} hash tag for cluster shard co-location
  - Lua scripts work correctly in cluster mode (all keys share same slot)
  - Set AEOS_REDIS_MODE=cluster to enable cluster mode
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from app.distributed.contracts.coordination import LeaseRecord, LeaseStore
from app.distributed.coordination.redis_client import create_redis_client, redis_key, RedisNamespace

logger = logging.getLogger(__name__)

_CLOCK_DRIFT_S = 2    # extra seconds added to TTL to tolerate NTP drift
_FENCING_SUFFIX = ":fence"


def _require_redis() -> Any:
    try:
        import redis.asyncio as aioredis
        return aioredis
    except ImportError as exc:
        raise ImportError(
            "redis[asyncio] is required for RedisLeaseStore. "
            "Install it with: pip install 'redis[asyncio]'"
        ) from exc


# Lua scripts — executed atomically on the Redis server
_RENEW_SCRIPT = """
local val = redis.call('GET', KEYS[1])
if not val then return 0 end
local data = cjson.decode(val)
if data['holder_id'] ~= ARGV[1] then return 0 end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
return 1
"""

_RELEASE_SCRIPT = """
local val = redis.call('GET', KEYS[1])
if not val then return 0 end
local data = cjson.decode(val)
if data['holder_id'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


class RedisLeaseStore(LeaseStore):
    """
    Redis-backed distributed lease store.

    All operations are atomic via Lua scripts (renew/release) or native
    Redis SET NX EX (acquire).

    Usage::

        store = RedisLeaseStore(url="redis://localhost:6379/0")
        await store.connect()
        lease = await store.acquire("my-key", "worker-1", ttl_seconds=30)
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        cluster_mode: bool | None = None,
    ) -> None:
        self._url = url
        self._cluster_mode = cluster_mode
        self._redis: Any = None
        self._renew_sha: str | None = None
        self._release_sha: str | None = None

    async def connect(self) -> None:
        # Use cluster-aware factory (resolves DRIFT-001)
        self._redis = await create_redis_client(
            self._url, cluster_mode=self._cluster_mode, decode_responses=False
        )
        # Pre-load Lua scripts
        self._renew_sha = await self._redis.script_load(_RENEW_SCRIPT)
        self._release_sha = await self._redis.script_load(_RELEASE_SCRIPT)
        logger.info("RedisLeaseStore: connected to %s (cluster=%s)",
                    self._url, self._cluster_mode)

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def acquire(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
        *,
        metadata: dict[str, str] | None = None,
    ) -> LeaseRecord | None:
        self._ensure_connected()
        acquired_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        value = json.dumps({
            "holder_id": holder_id,
            "acquired_at": acquired_at,
            "metadata": metadata or {},
        }).encode()
        effective_ttl = ttl_seconds + _CLOCK_DRIFT_S
        # Use cluster-safe key with hash tag
        rkey = redis_key(RedisNamespace.LEASE, lease_key).encode()
        # NX = only set if Not eXists; EX = expiry in seconds
        ok = await self._redis.set(rkey, value, nx=True, ex=effective_ttl)
        if not ok:
            return None

        # Increment fencing token (same hash tag = same shard)
        fence_rkey = redis_key(RedisNamespace.LEASE, lease_key + _FENCING_SUFFIX).encode()
        fence = await self._redis.incr(fence_rkey)
        logger.debug(
            "RedisLeaseStore: acquired '%s' by '%s' (ttl=%ds fence=%d)",
            lease_key, holder_id, effective_ttl, fence,
        )
        return LeaseRecord(
            lease_key=lease_key,
            holder_id=holder_id,
            ttl_seconds=ttl_seconds,
            acquired_at=acquired_at,
            metadata=metadata or {},
        )

    async def renew(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
    ) -> bool:
        self._ensure_connected()
        rkey = redis_key(RedisNamespace.LEASE, lease_key).encode()
        result = await self._redis.evalsha(
            self._renew_sha,
            1,
            rkey,
            holder_id.encode(),
            str(ttl_seconds + _CLOCK_DRIFT_S).encode(),
        )
        if result:
            logger.debug("RedisLeaseStore: renewed '%s' by '%s'", lease_key, holder_id)
        return bool(result)

    async def release(self, lease_key: str, holder_id: str) -> bool:
        self._ensure_connected()
        rkey = redis_key(RedisNamespace.LEASE, lease_key).encode()
        result = await self._redis.evalsha(
            self._release_sha,
            1,
            rkey,
            holder_id.encode(),
        )
        if result:
            logger.debug("RedisLeaseStore: released '%s' by '%s'", lease_key, holder_id)
        return bool(result)

    async def get(self, lease_key: str) -> LeaseRecord | None:
        self._ensure_connected()
        raw = await self._redis.get(redis_key(RedisNamespace.LEASE, lease_key).encode())
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return LeaseRecord(
                lease_key=lease_key,
                holder_id=data["holder_id"],
                ttl_seconds=0,  # TTL not stored in value
                acquired_at=data.get("acquired_at", ""),
                metadata=data.get("metadata", {}),
            )
        except (json.JSONDecodeError, KeyError):
            return None

    async def is_held_by(self, lease_key: str, holder_id: str) -> bool:
        record = await self.get(lease_key)
        return record is not None and record.holder_id == holder_id

    async def get_fencing_token(self, lease_key: str) -> int:
        """Returns the current fencing token value for this lease key."""
        self._ensure_connected()
        rkey = redis_key(RedisNamespace.LEASE, lease_key + _FENCING_SUFFIX).encode()
        raw = await self._redis.get(rkey)
        return int(raw) if raw else 0

    def _ensure_connected(self) -> None:
        if not self._redis:
            raise RuntimeError("RedisLeaseStore not connected — call connect() first")
