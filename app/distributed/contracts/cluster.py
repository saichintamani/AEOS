"""
Cluster membership contracts.

Defines the data types and interfaces for node identity, membership state
tracking, and the store that persists membership records.

State machine: SM-CLUSTER-MEMBER (017-STATE_MACHINE_SPECIFICATION.md)
Protocols: PROTO-001 (join), PROTO-002 (leave), PROTO-003 (failure detection)
Contract: AC-COMP-001, AC-LIFE-002
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Node Identity ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NodeIdentity:
    """
    Stable identity of a cluster node.

    Frozen so it can be used as a dict key and in sets. The node_id is
    generated once at process start and remains constant for the lifetime
    of the process. It is NOT persisted across restarts — a restarted node
    gets a new node_id and must re-join the cluster.

    Contract: AC-COMP-001 (every service has a node_id)
    """
    node_id: str = field(default_factory=_new_id)
    host: str = ""
    port: int = 0
    region: str = ""
    az: str = ""                     # availability zone
    version: str = "9.0.0"
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    @classmethod
    def from_env(cls, host: str, port: int) -> "NodeIdentity":
        """Create identity from runtime environment."""
        import socket
        return cls(
            node_id=str(uuid.uuid4()),
            host=host or socket.gethostname(),
            port=port,
        )


# ── Cluster Member State ──────────────────────────────────────────────────────

class ClusterMemberState(str, Enum):
    """
    Valid states of a cluster member.

    State machine: SM-CLUSTER-MEMBER
    JOINING   → RUNNING   (join complete)
    RUNNING   → SUSPECTED (3 missed heartbeats)
    SUSPECTED → RUNNING   (heartbeat received)
    SUSPECTED → FAILED    (5 missed heartbeats total)
    RUNNING   → DRAINING  (drain requested)
    DRAINING  → LEFT      (leave complete)

    FAILED and LEFT are terminal — recovery requires a new JOINING cycle.
    """
    JOINING   = "JOINING"
    RUNNING   = "RUNNING"
    SUSPECTED = "SUSPECTED"
    DRAINING  = "DRAINING"
    LEFT      = "LEFT"
    FAILED    = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (ClusterMemberState.LEFT, ClusterMemberState.FAILED)

    @property
    def is_healthy(self) -> bool:
        return self == ClusterMemberState.RUNNING

    @property
    def accepts_work(self) -> bool:
        """True when the node should receive new task assignments."""
        return self == ClusterMemberState.RUNNING


# ── Valid Transitions ─────────────────────────────────────────────────────────

_VALID_TRANSITIONS: dict[ClusterMemberState, set[ClusterMemberState]] = {
    ClusterMemberState.JOINING:   {ClusterMemberState.RUNNING, ClusterMemberState.FAILED},
    ClusterMemberState.RUNNING:   {ClusterMemberState.SUSPECTED, ClusterMemberState.DRAINING, ClusterMemberState.FAILED},
    ClusterMemberState.SUSPECTED: {ClusterMemberState.RUNNING, ClusterMemberState.FAILED},
    ClusterMemberState.DRAINING:  {ClusterMemberState.LEFT},
    ClusterMemberState.LEFT:      set(),
    ClusterMemberState.FAILED:    set(),
}


def validate_member_transition(
    from_state: ClusterMemberState,
    to_state: ClusterMemberState,
) -> None:
    """
    Raise StateMachineViolation if the transition is not permitted.

    Contract: SMR-002 (transition validation)
    Invariant: AC-LIFE-002 (no invalid transitions)
    """
    from app.distributed.cluster.exceptions import StateMachineViolation

    if to_state not in _VALID_TRANSITIONS.get(from_state, set()):
        raise StateMachineViolation(
            machine="SM-CLUSTER-MEMBER",
            from_state=from_state.value,
            to_state=to_state.value,
            event="explicit_transition",
        )


# ── Member Record ─────────────────────────────────────────────────────────────

@dataclass
class MemberRecord:
    """
    Runtime view of a cluster member, as known to the Cluster Manager.

    Updated on each heartbeat and state transition.
    Stored in the MembershipStore (authoritative: Raft log projection;
    Redis is a read-through cache with ≤5s staleness).

    Contract: AC-LIFE-002, INV-CONS-004
    """
    identity: NodeIdentity
    state: ClusterMemberState = ClusterMemberState.JOINING
    joined_at: str = field(default_factory=_now_iso)
    last_heartbeat_at: str = field(default_factory=_now_iso)
    missed_heartbeats: int = 0
    in_flight_tasks: int = 0
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    kafka_consumer_lag: int = 0
    assigned_partitions: list[int] = field(default_factory=list)

    @property
    def node_id(self) -> str:
        return self.identity.node_id

    def transition(self, to_state: ClusterMemberState) -> None:
        """Apply a validated state transition. Raises StateMachineViolation on illegal move."""
        validate_member_transition(self.state, to_state)
        self.state = to_state

    def record_heartbeat(
        self,
        *,
        in_flight_tasks: int = 0,
        cpu_utilization: float = 0.0,
        memory_utilization: float = 0.0,
        kafka_consumer_lag: int = 0,
    ) -> None:
        self.last_heartbeat_at = _now_iso()
        self.missed_heartbeats = 0
        self.in_flight_tasks = in_flight_tasks
        self.cpu_utilization = cpu_utilization
        self.memory_utilization = memory_utilization
        self.kafka_consumer_lag = kafka_consumer_lag

    def record_missed_heartbeat(self) -> None:
        self.missed_heartbeats += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.identity.node_id,
            "host": self.identity.host,
            "port": self.identity.port,
            "region": self.identity.region,
            "az": self.identity.az,
            "state": self.state.value,
            "joined_at": self.joined_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "missed_heartbeats": self.missed_heartbeats,
            "in_flight_tasks": self.in_flight_tasks,
            "cpu_utilization": self.cpu_utilization,
            "memory_utilization": self.memory_utilization,
            "kafka_consumer_lag": self.kafka_consumer_lag,
            "assigned_partitions": self.assigned_partitions,
        }


# ── Membership Store ABC ──────────────────────────────────────────────────────

class MembershipStore(ABC):
    """
    Persistent storage for cluster membership records.

    The authoritative implementation is a Raft log projection
    (Wave 9B.2). For Wave 9B.1, InMemoryMembershipStore is used.

    All write operations must be idempotent (safe to apply twice).
    Contract: AC-LIFE-002, INV-CONS-004
    """

    @abstractmethod
    async def add(self, record: MemberRecord) -> None:
        """Persist a new member record. Idempotent: upserts if already exists."""

    @abstractmethod
    async def update(self, record: MemberRecord) -> None:
        """Update an existing record. Raises KeyError if not found."""

    @abstractmethod
    async def get(self, node_id: str) -> MemberRecord | None:
        """Return the record for node_id, or None if not found."""

    @abstractmethod
    async def all(self) -> list[MemberRecord]:
        """Return all known member records."""

    @abstractmethod
    async def remove(self, node_id: str) -> None:
        """Remove a member record. No-op if not found."""

    @abstractmethod
    async def by_state(self, state: ClusterMemberState) -> list[MemberRecord]:
        """Return all members in the given state."""

    async def healthy_members(self) -> list[MemberRecord]:
        """Convenience: members that currently accept work."""
        return await self.by_state(ClusterMemberState.RUNNING)
