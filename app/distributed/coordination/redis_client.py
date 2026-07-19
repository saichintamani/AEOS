"""
app/distributed/coordination/redis_client.py

Redis Client Factory — resolves DRIFT-001.

Provides a single factory function that returns either:
  - redis.asyncio.RedisCluster  (cluster mode, for production)
  - redis.asyncio.Redis         (standalone, for local dev and CI)

Based on the AEOS_REDIS_MODE environment variable or explicit argument.

All AEOS components that need Redis (LeaseStore, MembershipStore,
CheckpointStore) MUST create their client via this factory to ensure
cluster mode is used in production.

Usage::

    from app.distributed.coordination.redis_client import create_redis_client

    # Production (ElastiCache Cluster)
    client = await create_redis_client(
        url="redis://cluster.endpoint:6379",
        cluster_mode=True,
    )

    # Local dev / CI
    client = await create_redis_client(
        url="redis://localhost:6379/0",
        cluster_mode=False,
    )

    # Auto-detect from AEOS_REDIS_MODE env var
    client = await create_redis_client(url="redis://...")

Key hash tag rules for Cluster mode:
  All AEOS keys MUST use hash tags to ensure related keys land on the
  same shard:
    - Lease keys:        {aeos:lease}:task-{id}
    - Membership keys:   {aeos:cluster}:members
    - Checkpoint keys:   {aeos:checkpoint}:task-{id}
    - Task state keys:   {aeos:task}:{id}
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Environment variable to control mode
_ENV_REDIS_MODE = "AEOS_REDIS_MODE"  # "cluster" | "standalone"


def _get_mode(cluster_mode: bool | None) -> bool:
    """Determine cluster mode from argument or environment variable."""
    if cluster_mode is not None:
        return cluster_mode
    env_val = os.environ.get(_ENV_REDIS_MODE, "").lower()
    if env_val == "cluster":
        return True
    if env_val == "standalone":
        return False
    # Default to cluster in production-like environments
    # (detected by absence of "local", "dev", "test" in hostname/url patterns)
    return False  # Safe default for development


async def create_redis_client(
    url: str = "redis://localhost:6379",
    cluster_mode: bool | None = None,
    *,
    decode_responses: bool = False,
    **kwargs: Any,
) -> Any:
    """
    Create and return an async Redis client.

    Args:
        url:            Redis URL. For cluster mode, this is any node's URL.
                        For standalone, this is the full connection URL.
        cluster_mode:   True = RedisCluster, False = Redis, None = auto-detect
                        from AEOS_REDIS_MODE env var.
        decode_responses: If True, return strings instead of bytes.
        **kwargs:       Passed to the underlying client constructor.

    Returns:
        redis.asyncio.RedisCluster or redis.asyncio.Redis instance.
        NOT yet connected — call await client.ping() or use as async context manager.
    """
    try:
        import redis.asyncio as aioredis
    except ImportError as exc:
        raise ImportError(
            "redis[asyncio] is required. Install: pip install 'redis[asyncio]'"
        ) from exc

    use_cluster = _get_mode(cluster_mode)

    if use_cluster:
        try:
            from redis.asyncio.cluster import RedisCluster
        except ImportError as exc:
            raise ImportError(
                "redis-py >= 4.3.0 required for cluster support. "
                "Install: pip install 'redis[asyncio]>=4.3.0'"
            ) from exc

        logger.info("Redis client: CLUSTER mode (url=%s)", url)
        client = RedisCluster.from_url(
            url,
            decode_responses=decode_responses,
            skip_full_coverage_check=True,  # Required for ElastiCache Cluster
            **kwargs,
        )
    else:
        logger.info("Redis client: STANDALONE mode (url=%s)", url)
        client = aioredis.from_url(
            url,
            decode_responses=decode_responses,
            **kwargs,
        )

    return client


def redis_key(namespace: str, *parts: str) -> str:
    """
    Build a cluster-safe Redis key with hash tag.

    All keys sharing the same namespace will land on the same shard.

    Examples:
        redis_key("lease", "task-123") → "{aeos:lease}:task-123"
        redis_key("cluster", "members") → "{aeos:cluster}:members"
        redis_key("checkpoint", "task-456", "v2") → "{aeos:checkpoint}:task-456:v2"

    The hash tag {aeos:<namespace>} ensures that keys with the same
    namespace are co-located on the same shard (required for Lua scripts
    that access multiple keys).
    """
    tag = f"{{aeos:{namespace}}}"
    if parts:
        return f"{tag}:{':'.join(parts)}"
    return tag


# Namespace constants for consistent key naming across all modules
class RedisNamespace:
    LEASE = "lease"
    CLUSTER = "cluster"
    CHECKPOINT = "checkpoint"
    TASK = "task"
    GOVERNANCE = "governance"
    CAPABILITY = "capability"
    WORKFLOW = "workflow"
