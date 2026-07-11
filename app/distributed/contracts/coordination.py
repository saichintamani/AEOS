"""
Coordination contracts — distributed clock, lease management, cluster metadata.

LeaseStore provides distributed mutual exclusion via SETNX-style leases.
DistributedClock provides a monotonic nanosecond clock for cross-topic ordering.
ClusterMetadata provides a read-through view of cluster-wide configuration.

Protocol: PROTO-019 (execution lease acquisition/renewal/release)
Contract: AC-CONS-001 (lease protocol), AC-OBS-002 (clock monotonicity)
ADR: ADR-009 (SETNX lease acquisition)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Lease Record ──────────────────────────────────────────────────────────────

@dataclass
class LeaseRecord:
    """
    Represents an acquired distributed lease.

    Contract: AC-CONS-001 (SETNX protocol)
    Protocol: PROTO-019
    """
    lease_key: str
    holder_id: str
    ttl_seconds: int
    acquired_at: str = field(default_factory=_now_iso)
    metadata: dict[str, str] = field(default_factory=dict)


# ── Lease Store ABC ───────────────────────────────────────────────────────────

class LeaseStore(ABC):
    """
    Distributed mutual-exclusion lease store.

    Semantics follow PROTO-019:
      1. acquire() → SETNX EX ttl_seconds; returns LeaseRecord on success, None on contention
      2. renew()   → EXPIRE the key if held by holder_id; raises LeaseNotHeld on mismatch
      3. release() → DEL the key if held by holder_id; no-op if already expired
      4. get()     → inspect current holder without acquiring

    Implementations: RedisLeaseStore (production), InMemoryLeaseStore (test/local).

    Contract: AC-CONS-001
    ADR: ADR-009
    """

    @abstractmethod
    async def acquire(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
        *,
        metadata: dict[str, str] | None = None,
    ) -> LeaseRecord | None:
        """Attempt to acquire a lease. Returns LeaseRecord on success, None on contention."""

    @abstractmethod
    async def renew(
        self,
        lease_key: str,
        holder_id: str,
        ttl_seconds: int = 120,
    ) -> bool:
        """Extend the TTL of a lease held by holder_id."""

    @abstractmethod
    async def release(
        self,
        lease_key: str,
        holder_id: str,
    ) -> bool:
        """Release a lease held by holder_id."""

    @abstractmethod
    async def get(self, lease_key: str) -> LeaseRecord | None:
        """Return the current lease record, or None if no lease exists."""

    @abstractmethod
    async def is_held_by(self, lease_key: str, holder_id: str) -> bool:
        """Return True if holder_id currently holds the named lease."""


# ── Distributed Clock ABC ─────────────────────────────────────────────────────

class DistributedClock(ABC):
    """
    Monotonic nanosecond clock for cross-topic event ordering.

    Contract: AC-OBS-002 (clock monotonicity invariant)
    §9.3 v1.1 spec: sequence_nanos used for cross-topic ordering.
    """

    @abstractmethod
    def now_nanos(self) -> int:
        """Return current time as nanoseconds. Guaranteed monotonically increasing within process."""

    @abstractmethod
    def now_iso(self) -> str:
        """Return current wall-clock time as ISO 8601 UTC string."""

    @property
    @abstractmethod
    def resolution_ns(self) -> int:
        """Minimum increment between successive now_nanos() calls."""


# ── Cluster Metadata ABC ──────────────────────────────────────────────────────

@dataclass
class TopicConfig:
    """Configuration for a single Kafka topic."""
    topic: str
    partitions: int
    replication_factor: int
    retention_ms: int = 604_800_000  # 7 days
    extra: dict[str, Any] = field(default_factory=dict)


class ClusterMetadata(ABC):
    """
    Read-through view of cluster-wide configuration and topology.

    Contract: AC-COMP-001 (every component can discover cluster topology)
    """

    @abstractmethod
    async def get_topic_config(self, topic: str) -> TopicConfig | None:
        """Return configuration for a named topic, or None if not found."""

    @abstractmethod
    async def list_topics(self) -> list[str]:
        """Return all known topic names."""

    @abstractmethod
    async def get_partition_count(self, topic: str) -> int:
        """Return the partition count for a topic. Raises KeyError if unknown."""

    @abstractmethod
    async def get_leader_node_id(self) -> str | None:
        """Return the current cluster leader node_id, or None if no leader."""

    @abstractmethod
    async def get_setting(self, key: str, default: Any = None) -> Any:
        """Return a cluster-wide configuration setting by key."""

    @abstractmethod
    async def set_setting(self, key: str, value: Any) -> None:
        """Persist a cluster-wide configuration setting."""

    @abstractmethod
    async def refresh(self) -> None:
        """Force a cache refresh from the authoritative source."""
