"""
Cluster member lifecycle manager.

Implements PROTO-001 (join), PROTO-002 (leave), PROTO-003 (failure detection).

Failure detection thresholds:
  _SUSPECT_AFTER_MISSED = 3  → mark SUSPECTED after 3 missed heartbeats
  _FAIL_AFTER_MISSED    = 5  → mark FAILED after 5 missed heartbeats
"""

from __future__ import annotations

import asyncio
import logging

from app.distributed.contracts.cluster import (
    ClusterMemberState,
    MemberRecord,
    MembershipStore,
    NodeIdentity,
)
from app.distributed.cluster.exceptions import DuplicateNodeError, NodeNotFound

logger = logging.getLogger(__name__)

_SUSPECT_AFTER_MISSED = 3
_FAIL_AFTER_MISSED = 5


class ClusterMemberManager:
    """
    Manages cluster membership lifecycle.

    Failure detection: background loop increments missed_heartbeat counters
    and transitions nodes through RUNNING → SUSPECTED → FAILED.
    """

    def __init__(
        self,
        store: MembershipStore,
        *,
        heartbeat_interval_seconds: float = 5.0,
        failure_check_interval_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._heartbeat_interval = heartbeat_interval_seconds
        self._check_interval = failure_check_interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._failure_detection_loop(), name="cluster-fd")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── PROTO-001: Join ───────────────────────────────────────────────────────

    async def join(self, identity: NodeIdentity) -> MemberRecord:
        """
        Register a new node as RUNNING. Raises DuplicateNodeError if already RUNNING.
        Idempotent for nodes in FAILED or LEFT state (re-join allowed).
        """
        existing = await self._store.get(identity.node_id)
        if existing and existing.state == ClusterMemberState.RUNNING:
            raise DuplicateNodeError(identity.node_id)
        record = MemberRecord(identity=identity, state=ClusterMemberState.RUNNING)
        await self._store.add(record)
        logger.info("Node joined: %s", identity.node_id)
        return record

    # ── PROTO-002: Leave ──────────────────────────────────────────────────────

    async def leave(self, node_id: str) -> None:
        """Gracefully remove a node from the cluster."""
        record = await self._store.get(node_id)
        if not record:
            raise NodeNotFound(node_id)
        record.state = ClusterMemberState.LEFT
        await self._store.update(record)
        await self._store.remove(node_id)
        logger.info("Node left: %s", node_id)

    # ── PROTO-003: Heartbeat / Failure Detection ──────────────────────────────

    async def record_heartbeat(self, node_id: str) -> None:
        """Reset missed heartbeat counter for a node."""
        record = await self._store.get(node_id)
        if record is None:
            return
        record.record_heartbeat()
        if record.state == ClusterMemberState.SUSPECTED:
            record.state = ClusterMemberState.RUNNING
        await self._store.update(record)

    async def _failure_detection_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._check_interval)
            try:
                await self._tick()
            except Exception:
                logger.exception("Failure detection tick error")

    async def _tick(self) -> None:
        running = await self._store.by_state(ClusterMemberState.RUNNING)
        suspected = await self._store.by_state(ClusterMemberState.SUSPECTED)
        for m in running + suspected:
            m.record_missed_heartbeat()
            if m.missed_heartbeats >= _FAIL_AFTER_MISSED:
                m.state = ClusterMemberState.FAILED
                logger.warning("Node FAILED (missed=%d): %s", m.missed_heartbeats, m.node_id)
            elif m.missed_heartbeats >= _SUSPECT_AFTER_MISSED:
                m.state = ClusterMemberState.SUSPECTED
                logger.warning("Node SUSPECTED (missed=%d): %s", m.missed_heartbeats, m.node_id)
            try:
                await self._store.update(m)
            except KeyError:
                pass

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_active_members(self) -> list[MemberRecord]:
        return await self._store.by_state(ClusterMemberState.RUNNING)

    async def get_member(self, node_id: str) -> MemberRecord | None:
        return await self._store.get(node_id)
