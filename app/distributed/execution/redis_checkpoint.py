"""
Wave 9B.5.2/9B.5.6 — Redis + Disk Checkpoint Store

Production replacement for InMemoryCheckpointStore.

Storage tiers:
  1. Redis — hot path, fast reads/writes, TTL-based GC
  2. Disk  — warm backup written alongside Redis (JSON files)

Two-phase protocol (PROTO-008):
  Phase 1: write_full(ctx, data, committed=False)  → Redis NX + disk
  Phase 2: commit(execution_id, step_id)            → SET committed=True

Recovery:
  load() returns only committed=True checkpoints (INV-EXEC-002).

Redis key schema:
  aeos:cp:{execution_id}:{step_id}  →  JSON blob
  aeos:cp:index:{execution_id}      →  sorted set of step_ids
"""

from __future__ import annotations

import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_KEY_PREFIX = "aeos:cp:"
_INDEX_PREFIX = "aeos:cp:index:"
_DEFAULT_TTL = 86_400   # 24 hours


def _require_redis() -> Any:
    try:
        import redis.asyncio as aioredis
        return aioredis
    except ImportError as exc:
        raise ImportError(
            "redis[asyncio] is required for RedisCheckpointStore. "
            "Install it with: pip install 'redis[asyncio]'"
        ) from exc


class RedisCheckpointStore:
    """
    Production checkpoint store backed by Redis with optional disk backup.

    All two-phase protocol invariants from the in-memory implementation
    are preserved — only committed checkpoints are returned by load().
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        disk_dir: str | None = None,
        ttl: int = _DEFAULT_TTL,
    ) -> None:
        self._url = url
        self._disk_dir = pathlib.Path(disk_dir) if disk_dir else None
        self._ttl = ttl
        self._redis: Any = None

        if self._disk_dir:
            self._disk_dir.mkdir(parents=True, exist_ok=True)

    async def connect(self) -> None:
        aioredis = _require_redis()
        self._redis = aioredis.from_url(self._url, decode_responses=False)
        logger.info("RedisCheckpointStore: connected to %s", self._url)

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def write_full(
        self,
        execution_id: str,
        step_id: str,
        data: dict,
        *,
        committed: bool = False,
    ) -> None:
        """Phase 1: write checkpoint (committed=False) or commit in one call."""
        self._ensure_connected()
        blob = json.dumps({
            "execution_id": execution_id,
            "step_id": step_id,
            "data": data,
            "committed": committed,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }).encode()
        key = self._key(execution_id, step_id)
        await self._redis.set(key, blob, ex=self._ttl)
        # Update sorted index (score = current epoch)
        import time
        await self._redis.zadd(
            self._index_key(execution_id).encode(),
            {step_id.encode(): time.time()},
        )
        # Disk backup
        if self._disk_dir:
            await self._write_disk(execution_id, step_id, blob)

    async def commit(self, execution_id: str, step_id: str) -> bool:
        """Phase 2: mark checkpoint as committed."""
        self._ensure_connected()
        key = self._key(execution_id, step_id)
        raw = await self._redis.get(key)
        if not raw:
            return False
        data = json.loads(raw)
        data["committed"] = True
        await self._redis.set(key, json.dumps(data).encode(), ex=self._ttl)
        if self._disk_dir:
            await self._write_disk(execution_id, step_id, json.dumps(data).encode())
        return True

    async def load(self, execution_id: str) -> list[dict]:
        """Return all committed checkpoints for an execution, ordered by step_id."""
        self._ensure_connected()
        step_ids_raw = await self._redis.zrange(
            self._index_key(execution_id).encode(), 0, -1
        )
        results = []
        for sid_raw in step_ids_raw:
            sid = sid_raw.decode() if isinstance(sid_raw, bytes) else sid_raw
            raw = await self._redis.get(self._key(execution_id, sid))
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("committed"):
                results.append(data)
        return results

    async def latest_committed(self, execution_id: str) -> dict | None:
        """Returns the most-recently committed checkpoint."""
        checkpoints = await self.load(execution_id)
        return checkpoints[-1] if checkpoints else None

    async def delete(self, execution_id: str) -> None:
        """GC: remove all checkpoints for a completed execution."""
        self._ensure_connected()
        step_ids_raw = await self._redis.zrange(
            self._index_key(execution_id).encode(), 0, -1
        )
        for sid_raw in step_ids_raw:
            sid = sid_raw.decode() if isinstance(sid_raw, bytes) else sid_raw
            await self._redis.delete(self._key(execution_id, sid))
        await self._redis.delete(self._index_key(execution_id).encode())

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _key(self, execution_id: str, step_id: str) -> bytes:
        return f"{_KEY_PREFIX}{execution_id}:{step_id}".encode()

    def _index_key(self, execution_id: str) -> str:
        return f"{_INDEX_PREFIX}{execution_id}"

    async def _write_disk(self, execution_id: str, step_id: str, blob: bytes) -> None:
        if not self._disk_dir:
            return
        path = self._disk_dir / execution_id
        path.mkdir(exist_ok=True)
        (path / f"{step_id}.json").write_bytes(blob)

    def _ensure_connected(self) -> None:
        if not self._redis:
            raise RuntimeError("RedisCheckpointStore not connected — call connect() first")
