"""
Phase 9B.6 — State Machine Validator

Enforces state machine transitions as defined in 017-STATE_MACHINE_SPECIFICATION.md.

Every AEOS subsystem with observable state must pass through a defined
transition table. Invalid transitions raise StateMachineViolation and
are recorded in the violation log.

State Machines implemented:
  SM-TASK            — Task execution lifecycle (7 states)
  SM-CLUSTER-MEMBER  — Cluster member lifecycle (6 states, referenced by cluster.py)
  SM-CHECKPOINT      — Checkpoint lifecycle (4 states)
  SM-CAPABILITY      — Capability advertisement lifecycle (4 states)
  SM-CIRCUIT-BREAKER — Circuit breaker pattern (3 states)
  SM-RAFT            — Raft node role (3 states)
  SM-WORKFLOW        — Workflow lifecycle (6 states)

Usage::

    validator = StateMachineValidator()
    validator.transition("SM-TASK", "PENDING", "RUNNING", context={"task_id": "t1"})
    # Raises StateMachineViolation if the transition is invalid
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Exception ─────────────────────────────────────────────────────────────────

@dataclass
class StateMachineViolation(Exception):
    machine: str
    from_state: str
    to_state: str
    event: str = ""
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"StateMachineViolation[{self.machine}]: "
            f"{self.from_state} → {self.to_state} is not permitted"
            + (f" (event={self.event})" if self.event else "")
        )

    def __post_init__(self):
        super().__init__(str(self))


# ── Transition record ─────────────────────────────────────────────────────────

@dataclass
class TransitionRecord:
    machine: str
    from_state: str
    to_state: str
    event: str
    valid: bool
    context: dict[str, Any] = field(default_factory=dict)
    recorded_at: float = field(default_factory=time.monotonic)


# ── State Machine Definitions ──────────────────────────────────────────────────
# Format: machine_id → {from_state → set of allowed to_states}

_TRANSITIONS: dict[str, dict[str, set[str]]] = {

    # SM-TASK — Task Execution (017-STATE_MACHINE_SPECIFICATION.md)
    "SM-TASK": {
        "PENDING":    {"SCHEDULED", "CANCELLED", "FAILED"},
        "SCHEDULED":  {"RUNNING", "CANCELLED", "FAILED"},
        "RUNNING":    {"COMPLETED", "FAILED", "TIMEOUT", "SUSPENDED"},
        "SUSPENDED":  {"RUNNING", "FAILED", "CANCELLED"},
        "COMPLETED":  set(),   # terminal
        "FAILED":     set(),   # terminal
        "CANCELLED":  set(),   # terminal
        "TIMEOUT":    set(),   # terminal
    },

    # SM-CLUSTER-MEMBER — mirrors cluster.py _VALID_TRANSITIONS
    "SM-CLUSTER-MEMBER": {
        "JOINING":   {"RUNNING", "FAILED"},
        "RUNNING":   {"SUSPECTED", "DRAINING", "FAILED"},
        "SUSPECTED": {"RUNNING", "FAILED"},
        "DRAINING":  {"LEFT"},
        "LEFT":      set(),    # terminal
        "FAILED":    set(),    # terminal
    },

    # SM-CHECKPOINT
    "SM-CHECKPOINT": {
        "PENDING":   {"PHASE1_WRITTEN", "FAILED"},
        "PHASE1_WRITTEN": {"COMMITTED", "FAILED"},
        "COMMITTED": set(),    # terminal
        "FAILED":    set(),    # terminal
    },

    # SM-CAPABILITY
    "SM-CAPABILITY": {
        "UNREGISTERED": {"ACTIVE"},
        "ACTIVE":       {"REFRESHING", "STALE", "DEREGISTERED"},
        "REFRESHING":   {"ACTIVE", "STALE"},
        "STALE":        {"ACTIVE", "DEREGISTERED"},
        "DEREGISTERED": set(),   # terminal
    },

    # SM-CIRCUIT-BREAKER
    "SM-CIRCUIT-BREAKER": {
        "CLOSED":    {"OPEN"},
        "OPEN":      {"HALF_OPEN"},
        "HALF_OPEN": {"CLOSED", "OPEN"},
    },

    # SM-RAFT
    "SM-RAFT": {
        "FOLLOWER":  {"CANDIDATE"},
        "CANDIDATE": {"LEADER", "FOLLOWER"},
        "LEADER":    {"FOLLOWER"},
    },

    # SM-WORKFLOW
    "SM-WORKFLOW": {
        "PENDING":    {"RUNNING", "CANCELLED", "FAILED"},
        "RUNNING":    {"COMPLETED", "FAILED", "PAUSED", "CANCELLED"},
        "PAUSED":     {"RUNNING", "CANCELLED"},
        "COMPLETED":  set(),   # terminal
        "FAILED":     set(),   # terminal
        "CANCELLED":  set(),   # terminal
    },

    # SM-GOVERNANCE-TOKEN
    "SM-GOVERNANCE": {
        "ISSUED":   {"ACTIVE"},
        "ACTIVE":   {"APPROVED", "REJECTED", "EXPIRED", "REVOKED"},
        "APPROVED": set(),   # terminal
        "REJECTED": set(),   # terminal
        "EXPIRED":  set(),   # terminal
        "REVOKED":  set(),   # terminal
    },
}

# States that are terminal (no outgoing transitions)
_TERMINAL_STATES: dict[str, set[str]] = {
    machine: {s for s, nexts in trans.items() if not nexts}
    for machine, trans in _TRANSITIONS.items()
}


# ── State Machine Instance ────────────────────────────────────────────────────

class StateMachine:
    """
    Tracks the current state of a single entity and validates transitions.

    Usage::

        sm = StateMachine("SM-TASK", initial_state="PENDING")
        sm.transition("RUNNING", event="dispatch")   # ok
        sm.transition("PENDING", event="backward")   # raises StateMachineViolation
    """

    def __init__(
        self,
        machine_id: str,
        initial_state: str,
        entity_id: str = "",
    ) -> None:
        if machine_id not in _TRANSITIONS:
            raise ValueError(f"Unknown state machine: {machine_id}")
        self._machine = machine_id
        self._state = initial_state
        self._entity_id = entity_id
        self._history: list[TransitionRecord] = []

    @property
    def state(self) -> str:
        return self._state

    @property
    def machine_id(self) -> str:
        return self._machine

    @property
    def is_terminal(self) -> bool:
        return self._state in _TERMINAL_STATES.get(self._machine, set())

    def transition(
        self,
        to_state: str,
        *,
        event: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        """
        Attempt a transition to `to_state`.

        Raises StateMachineViolation if the transition is not permitted.
        Logs all transitions (valid or not) to history.
        """
        allowed = _TRANSITIONS.get(self._machine, {}).get(self._state, set())
        valid = to_state in allowed

        record = TransitionRecord(
            machine=self._machine,
            from_state=self._state,
            to_state=to_state,
            event=event,
            valid=valid,
            context={"entity_id": self._entity_id, **(context or {})},
        )
        self._history.append(record)

        if not valid:
            exc = StateMachineViolation(
                machine=self._machine,
                from_state=self._state,
                to_state=to_state,
                event=event,
                context={"entity_id": self._entity_id, **(context or {})},
            )
            logger.error("STATE_MACHINE_VIOLATION: %s", exc)
            raise exc

        logger.debug(
            "SM[%s] %s: %s → %s (event=%s)",
            self._machine, self._entity_id, self._state, to_state, event,
        )
        self._state = to_state

    @property
    def history(self) -> list[TransitionRecord]:
        return list(self._history)

    def can_transition_to(self, to_state: str) -> bool:
        allowed = _TRANSITIONS.get(self._machine, {}).get(self._state, set())
        return to_state in allowed


# ── Validator ─────────────────────────────────────────────────────────────────

class StateMachineValidator:
    """
    Validates state transitions against the architecture spec and records
    all transitions (valid and invalid) for audit.

    Unlike StateMachine (which tracks one entity), this validator is
    a central service that multiple entity state machines report to.
    It does not hold state itself — it validates transitions on-demand.
    """

    def __init__(self) -> None:
        self._records: list[TransitionRecord] = []

    def validate(
        self,
        machine_id: str,
        from_state: str,
        to_state: str,
        *,
        event: str = "",
        context: dict[str, Any] | None = None,
        raise_on_violation: bool = True,
    ) -> bool:
        """
        Validate a proposed transition.

        Returns True if valid, False if not.
        Raises StateMachineViolation if raise_on_violation=True (default).
        """
        allowed = _TRANSITIONS.get(machine_id, {}).get(from_state, set())
        valid = to_state in allowed

        record = TransitionRecord(
            machine=machine_id,
            from_state=from_state,
            to_state=to_state,
            event=event,
            valid=valid,
            context=context or {},
        )
        self._records.append(record)

        if not valid:
            if machine_id not in _TRANSITIONS:
                raise ValueError(f"Unknown state machine: '{machine_id}'")
            exc = StateMachineViolation(
                machine=machine_id,
                from_state=from_state,
                to_state=to_state,
                event=event,
                context=context or {},
            )
            logger.error("STATE_MACHINE_VIOLATION: %s", exc)
            if raise_on_violation:
                raise exc

        return valid

    def is_terminal(self, machine_id: str, state: str) -> bool:
        return state in _TERMINAL_STATES.get(machine_id, set())

    def allowed_transitions(self, machine_id: str, from_state: str) -> set[str]:
        return _TRANSITIONS.get(machine_id, {}).get(from_state, set())

    @property
    def violation_count(self) -> int:
        return sum(1 for r in self._records if not r.valid)

    @property
    def audit_log(self) -> list[TransitionRecord]:
        return list(self._records)

    def violations(self) -> list[TransitionRecord]:
        return [r for r in self._records if not r.valid]

    @staticmethod
    def machines() -> list[str]:
        return list(_TRANSITIONS.keys())
