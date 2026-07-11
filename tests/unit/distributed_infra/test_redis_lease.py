"""
Unit tests for Wave 9B.5.2 — RedisLeaseStore.

All redis.asyncio dependencies are mocked.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_redis_mock():
    r = AsyncMock()
    r.script_load = AsyncMock(return_value="abc123")
    r.set = AsyncMock(return_value=True)
    r.get = AsyncMock(return_value=None)
    r.incr = AsyncMock(return_value=1)
    r.evalsha = AsyncMock(return_value=1)
    r.aclose = AsyncMock()
    return r


def _make_lease_json(holder_id: str = "worker-1") -> bytes:
    return json.dumps({
        "holder_id": holder_id,
        "acquired_at": "2024-01-01T00:00:00Z",
        "metadata": {},
    }).encode()


class TestRedisLeaseAcquire:

    @pytest.mark.asyncio
    async def test_acquire_success_returns_record(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.set = AsyncMock(return_value=True)
        redis.incr = AsyncMock(return_value=1)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        record = await store.acquire("my-key", "worker-1", ttl_seconds=30)
        assert record is not None
        assert record.lease_key == "my-key"
        assert record.holder_id == "worker-1"

    @pytest.mark.asyncio
    async def test_acquire_returns_none_when_already_held(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.set = AsyncMock(return_value=None)   # NX failed

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        record = await store.acquire("my-key", "worker-2", ttl_seconds=30)
        assert record is None

    @pytest.mark.asyncio
    async def test_acquire_increments_fencing_token(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.set = AsyncMock(return_value=True)
        redis.incr = AsyncMock(return_value=5)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        record = await store.acquire("fence-key", "worker-1")
        assert record is not None
        redis.incr.assert_called_once()
        # Key includes :fence suffix
        fence_key_arg = redis.incr.call_args[0][0]
        assert b":fence" in fence_key_arg

    @pytest.mark.asyncio
    async def test_acquire_adds_clock_drift_to_ttl(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore, _CLOCK_DRIFT_S

        redis = _make_redis_mock()
        redis.set = AsyncMock(return_value=True)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"

        await store.acquire("ttl-key", "w1", ttl_seconds=30)
        call_kwargs = redis.set.call_args[1]
        assert call_kwargs.get("ex") == 30 + _CLOCK_DRIFT_S

    @pytest.mark.asyncio
    async def test_acquire_not_connected_raises(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._url = "redis://localhost"
        store._redis = None

        with pytest.raises(RuntimeError):
            await store.acquire("key", "holder")


class TestRedisLeaseRenew:

    @pytest.mark.asyncio
    async def test_renew_returns_true_on_success(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.evalsha = AsyncMock(return_value=1)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        ok = await store.renew("my-key", "worker-1", 60)
        assert ok is True
        redis.evalsha.assert_called_once()
        assert redis.evalsha.call_args[0][0] == "sha-renew"

    @pytest.mark.asyncio
    async def test_renew_returns_false_when_not_holder(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.evalsha = AsyncMock(return_value=0)   # Lua returned 0

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        ok = await store.renew("my-key", "wrong-holder", 60)
        assert ok is False


class TestRedisLeaseRelease:

    @pytest.mark.asyncio
    async def test_release_returns_true_on_success(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.evalsha = AsyncMock(return_value=1)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        ok = await store.release("my-key", "worker-1")
        assert ok is True
        assert redis.evalsha.call_args[0][0] == "sha-release"

    @pytest.mark.asyncio
    async def test_release_returns_false_when_not_holder(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.evalsha = AsyncMock(return_value=0)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha-renew"
        store._release_sha = "sha-release"

        ok = await store.release("my-key", "wrong-holder")
        assert ok is False


class TestRedisLeaseGet:

    @pytest.mark.asyncio
    async def test_get_returns_record_when_present(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=_make_lease_json("worker-1"))

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"

        record = await store.get("my-key")
        assert record is not None
        assert record.holder_id == "worker-1"
        assert record.lease_key == "my-key"

    @pytest.mark.asyncio
    async def test_get_returns_none_when_missing(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=None)

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"

        record = await store.get("missing-key")
        assert record is None

    @pytest.mark.asyncio
    async def test_is_held_by_true(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=_make_lease_json("worker-1"))

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"

        assert await store.is_held_by("key", "worker-1") is True
        assert await store.is_held_by("key", "worker-2") is False

    @pytest.mark.asyncio
    async def test_fencing_token_returned(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=b"7")

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"

        token = await store.get_fencing_token("my-key")
        assert token == 7


class TestRedisLeaseDisconnect:

    @pytest.mark.asyncio
    async def test_disconnect_closes_connection(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        redis = _make_redis_mock()
        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = redis
        store._renew_sha = "sha"
        store._release_sha = "sha"
        store._url = "redis://localhost"

        await store.disconnect()
        redis.aclose.assert_called_once()
        assert store._redis is None

    @pytest.mark.asyncio
    async def test_disconnect_noop_when_not_connected(self):
        from app.distributed.coordination.redis_lease import RedisLeaseStore

        store = RedisLeaseStore.__new__(RedisLeaseStore)
        store._redis = None
        await store.disconnect()   # should not raise
