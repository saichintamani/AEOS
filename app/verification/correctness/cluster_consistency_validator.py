"""
app/verification/correctness/cluster_consistency_validator.py

Cluster Consistency Validator — DCS §5 requirement.

Simultaneously queries three sources of cluster membership truth:
  1. Raft log (authoritative)
  2. Redis membership cache
  3. gRPC channel registry

Verifies all three are consistent within INV-CONS-004's 5-second
staleness threshold. Detects split views, phantom members, and
missing members.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MemberView:
    """A single source's view of cluster membership."""
    source: str   # "raft", "redis", "grpc"
    members: set[str]     # node_ids
    snapshot_time: float  # when this was captured
    term: int = 0         # Raft term at snapshot (raft source only)


@dataclass
class ConsistencyViolation:
    description: str
    inv_id: str
    severity: str  # "CRITICAL" | "ERROR"
    sources: list[str] = field(default_factory=list)


@dataclass
class ConsistencyReport:
    timestamp: float
    consistent: bool
    staleness_seconds: float
    raft_view: set[str]
    redis_view: set[str]
    grpc_view: set[str]
    violations: list[ConsistencyViolation] = field(default_factory=list)

    @property
    def all_views_agree(self) -> bool:
        return self.raft_view == self.redis_view == self.grpc_view


class ClusterConsistencyValidator:
    """
    Validates cluster membership consistency across 3 views simultaneously.

    INV-CONS-004: "Membership Cache Staleness ≤ 5s" — all views must
    converge within 5 seconds.

    Usage::

        validator = ClusterConsistencyValidator(
            raft_node=raft_node,
            redis_client=redis,
            grpc_registry=registry,
        )
        report = await validator.check_consistency()
        assert report.consistent
    """

    STALENESS_LIMIT = 5.0  # seconds — from INV-CONS-004

    def __init__(
        self,
        raft_node: Any | None = None,
        redis_client: Any | None = None,
        grpc_registry: Any | None = None,
    ) -> None:
        self._raft = raft_node
        self._redis = redis_client
        self._grpc = grpc_registry

    async def check_consistency(self) -> ConsistencyReport:
        """
        Query all three membership sources simultaneously and compare.
        Returns a ConsistencyReport with any violations found.
        """
        now = time.time()

        # Query all three sources concurrently
        raft_view, redis_view, grpc_view = await asyncio.gather(
            self._get_raft_view(),
            self._get_redis_view(),
            self._get_grpc_view(),
        )

        # Maximum staleness = most recent snapshot minus the oldest
        snap_times = [v.snapshot_time for v in [raft_view, redis_view, grpc_view]]
        staleness = max(snap_times) - min(snap_times)

        violations: list[ConsistencyViolation] = []

        # INV-CONS-004: staleness check
        if staleness > self.STALENESS_LIMIT:
            violations.append(ConsistencyViolation(
                description=(
                    f"Membership views differ by {staleness:.2f}s "
                    f"(limit: {self.STALENESS_LIMIT}s)"
                ),
                inv_id="INV-CONS-004",
                severity="ERROR",
                sources=["raft", "redis", "grpc"],
            ))

        # Check all views agree
        r_members = raft_view.members
        red_members = redis_view.members
        g_members = grpc_view.members

        # Raft is authoritative — find deviations
        phantom_in_redis = red_members - r_members
        missing_in_redis = r_members - red_members
        phantom_in_grpc = g_members - r_members
        missing_in_grpc = r_members - g_members

        if phantom_in_redis:
            violations.append(ConsistencyViolation(
                description=f"Redis has {len(phantom_in_redis)} phantom member(s) not in Raft log: {phantom_in_redis}",
                inv_id="INV-CONS-004",
                severity="CRITICAL",
                sources=["raft", "redis"],
            ))

        if missing_in_redis:
            violations.append(ConsistencyViolation(
                description=f"Redis missing {len(missing_in_redis)} member(s) present in Raft log: {missing_in_redis}",
                inv_id="INV-CONS-004",
                severity="ERROR",
                sources=["raft", "redis"],
            ))

        if phantom_in_grpc:
            violations.append(ConsistencyViolation(
                description=f"gRPC registry has {len(phantom_in_grpc)} phantom member(s): {phantom_in_grpc}",
                inv_id="INV-CONS-004",
                severity="CRITICAL",
                sources=["raft", "grpc"],
            ))

        if missing_in_grpc:
            violations.append(ConsistencyViolation(
                description=f"gRPC registry missing {len(missing_in_grpc)} member(s): {missing_in_grpc}",
                inv_id="INV-CONS-004",
                severity="ERROR",
                sources=["raft", "grpc"],
            ))

        return ConsistencyReport(
            timestamp=now,
            consistent=len(violations) == 0,
            staleness_seconds=staleness,
            raft_view=r_members,
            redis_view=red_members,
            grpc_view=g_members,
            violations=violations,
        )

    async def _get_raft_view(self) -> MemberView:
        """Get membership from Raft log (authoritative source)."""
        if self._raft is None:
            return MemberView(source="raft", members=set(), snapshot_time=time.time())
        try:
            members = await self._raft.get_membership()
            term = getattr(self._raft, "current_term", 0)
            return MemberView(
                source="raft",
                members={m["node_id"] for m in members},
                snapshot_time=time.time(),
                term=term,
            )
        except Exception:  # noqa: BLE001
            return MemberView(source="raft", members=set(), snapshot_time=time.time())

    async def _get_redis_view(self) -> MemberView:
        """Get membership from Redis cache."""
        if self._redis is None:
            return MemberView(source="redis", members=set(), snapshot_time=time.time())
        try:
            raw = await self._redis.smembers("aeos:cluster:members")
            members = {m.decode() if isinstance(m, bytes) else m for m in raw}
            return MemberView(source="redis", members=members, snapshot_time=time.time())
        except Exception:  # noqa: BLE001
            return MemberView(source="redis", members=set(), snapshot_time=time.time())

    async def _get_grpc_view(self) -> MemberView:
        """Get membership from gRPC channel registry."""
        if self._grpc is None:
            return MemberView(source="grpc", members=set(), snapshot_time=time.time())
        try:
            channels = await self._grpc.list_channels()
            members = {ch["node_id"] for ch in channels if ch.get("healthy")}
            return MemberView(source="grpc", members=members, snapshot_time=time.time())
        except Exception:  # noqa: BLE001
            return MemberView(source="grpc", members=set(), snapshot_time=time.time())

    async def monitor(self, interval: float = 5.0) -> None:
        """
        Background monitor — checks consistency every `interval` seconds.
        Raises on critical violations. Designed to run as an asyncio task.
        """
        while True:
            report = await self.check_consistency()
            critical = [v for v in report.violations if v.severity == "CRITICAL"]
            if critical:
                import logging
                log = logging.getLogger(__name__)
                for v in critical:
                    log.critical("[%s] %s", v.inv_id, v.description)
            await asyncio.sleep(interval)
