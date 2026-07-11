"""
In-memory cluster metadata store.

Pre-populated with all 9 standard AEOS topics defined in §8.1 of the spec.
Production: RedisClusterMetadata backed by Kafka AdminClient.

Contract: AC-COMP-001
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.distributed.contracts.coordination import ClusterMetadata, TopicConfig


_DEFAULT_TOPICS: list[TopicConfig] = [
    TopicConfig("aeos.events.cluster",     partitions=6,  replication_factor=3, retention_ms=86_400_000),
    TopicConfig("aeos.events.execution",   partitions=12, replication_factor=3),
    TopicConfig("aeos.events.governance",  partitions=3,  replication_factor=3, retention_ms=2_592_000_000),
    TopicConfig("aeos.events.workflow",    partitions=12, replication_factor=3),
    TopicConfig("aeos.events.agent",       partitions=12, replication_factor=3),
    TopicConfig("aeos.events.task",        partitions=24, replication_factor=3),
    TopicConfig("aeos.events.memory",      partitions=6,  replication_factor=3),
    TopicConfig("aeos.events.metrics",     partitions=6,  replication_factor=3, retention_ms=86_400_000),
    TopicConfig("aeos.events.deadletter",  partitions=3,  replication_factor=3, retention_ms=2_592_000_000),
]


class InMemoryClusterMetadata(ClusterMetadata):
    """
    Dict-backed cluster metadata for testing and local deployments.

    Initialised with all standard AEOS topics. Callers can add topics via
    add_topic() to simulate dynamic topic creation.
    """

    def __init__(self) -> None:
        self._topics: dict[str, TopicConfig] = {
            t.topic: t for t in _DEFAULT_TOPICS
        }
        self._settings: dict[str, Any] = {}
        self._leader_node_id: str | None = None
        self._lock = asyncio.Lock()

    async def get_topic_config(self, topic: str) -> TopicConfig | None:
        async with self._lock:
            return self._topics.get(topic)

    async def list_topics(self) -> list[str]:
        async with self._lock:
            return list(self._topics.keys())

    async def get_partition_count(self, topic: str) -> int:
        async with self._lock:
            cfg = self._topics.get(topic)
            if cfg is None:
                raise KeyError(f"Unknown topic: {topic!r}")
            return cfg.partitions

    async def get_leader_node_id(self) -> str | None:
        async with self._lock:
            return self._leader_node_id

    async def get_setting(self, key: str, default: Any = None) -> Any:
        async with self._lock:
            return self._settings.get(key, default)

    async def set_setting(self, key: str, value: Any) -> None:
        async with self._lock:
            self._settings[key] = value

    async def refresh(self) -> None:
        pass  # no-op for in-memory; production would re-fetch from Kafka AdminClient

    # ── Test helpers ──────────────────────────────────────────────────────────

    async def add_topic(self, config: TopicConfig) -> None:
        async with self._lock:
            self._topics[config.topic] = config

    async def set_leader(self, node_id: str | None) -> None:
        async with self._lock:
            self._leader_node_id = node_id
