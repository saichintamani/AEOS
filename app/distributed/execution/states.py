"""
SM-TASK: Execution State Machine — 11 states, deterministic transitions.

Contract: AC-EXEC-001
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ExecutionState(str, Enum):
    CREATED        = "CREATED"
    QUEUED         = "QUEUED"
    LEASED         = "LEASED"
    SCHEDULED      = "SCHEDULED"
    RUNNING        = "RUNNING"
    CHECKPOINTING  = "CHECKPOINTING"
    PAUSED         = "PAUSED"
    RECOVERING     = "RECOVERING"
    COMPLETED      = "COMPLETED"
    FAILED         = "FAILED"
    CANCELLED      = "CANCELLED"


VALID_EXECUTION_TRANSITIONS: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.CREATED:       {ExecutionState.QUEUED},
    ExecutionState.QUEUED:        {ExecutionState.LEASED, ExecutionState.CANCELLED},
    ExecutionState.LEASED:        {ExecutionState.SCHEDULED, ExecutionState.QUEUED, ExecutionState.CANCELLED},
    ExecutionState.SCHEDULED:     {ExecutionState.RUNNING, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.RUNNING:       {ExecutionState.CHECKPOINTING, ExecutionState.PAUSED,
                                   ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED},
    ExecutionState.CHECKPOINTING: {ExecutionState.RUNNING, ExecutionState.FAILED},
    ExecutionState.PAUSED:        {ExecutionState.RECOVERING, ExecutionState.CANCELLED},
    ExecutionState.RECOVERING:    {ExecutionState.RUNNING, ExecutionState.FAILED},
    ExecutionState.COMPLETED:     set(),
    ExecutionState.FAILED:        {ExecutionState.QUEUED},  # re-queue for retry
    ExecutionState.CANCELLED:     set(),
}


@dataclass
class ExecutionTransition:
    from_state: ExecutionState
    to_state: ExecutionState
    event: str


class InvalidTransitionError(Exception):
    def __init__(self, from_state: ExecutionState, to_state: ExecutionState, event: str = "explicit") -> None:
        super().__init__(
            f"SM-TASK: {from_state.value} → {to_state.value} not allowed (event={event!r})"
        )
        self.from_state = from_state
        self.to_state = to_state
        self.event = event


def validate_execution_transition(
    from_state: ExecutionState,
    to_state: ExecutionState,
    event: str = "explicit",
) -> ExecutionTransition:
    allowed = VALID_EXECUTION_TRANSITIONS.get(from_state, set())
    if to_state not in allowed:
        raise InvalidTransitionError(from_state, to_state, event)
    return ExecutionTransition(from_state=from_state, to_state=to_state, event=event)
