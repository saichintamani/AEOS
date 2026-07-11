"""
Cluster-specific exception hierarchy.

Protocol: PROTO-001 (join), PROTO-002 (leave), PROTO-003 (failure detection)
"""

from __future__ import annotations


class ClusterError(Exception):
    """Base class for all cluster-related errors."""


class StateMachineViolation(ClusterError):
    """Raised when a state transition is not allowed."""

    def __init__(
        self,
        machine: str,
        from_state: str,
        to_state: str,
        event: str = "explicit",
    ) -> None:
        self.machine = machine
        self.from_state = from_state
        self.to_state = to_state
        self.event = event
        super().__init__(
            f"{machine}: {from_state!r} → {to_state!r} not allowed (event={event!r})"
        )


class LeaseNotHeld(ClusterError):
    """Raised when a lease operation is attempted by a non-holder."""

    def __init__(
        self,
        lease_key: str,
        expected_holder: str,
        actual_holder: str | None,
    ) -> None:
        self.lease_key = lease_key
        self.expected_holder = expected_holder
        self.actual_holder = actual_holder
        super().__init__(
            f"Lease {lease_key!r} held by {actual_holder!r}, not {expected_holder!r}"
        )


class NodeNotFound(ClusterError):
    """Raised when a referenced node does not exist in the membership store."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"Node {node_id!r} not found in membership store")


class DuplicateNodeError(ClusterError):
    """Raised when a node attempts to join but is already registered as ACTIVE."""

    def __init__(self, node_id: str) -> None:
        self.node_id = node_id
        super().__init__(f"Node {node_id!r} is already a member of the cluster")
