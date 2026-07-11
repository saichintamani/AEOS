"""
ExecutionContext — authoritative runtime record for a single task execution.
CheckpointData — serializable execution snapshot.

execution_id is re-minted on each retry so old checkpoints are distinguishable.

Contract: AC-EXEC-001
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.distributed.execution.states import ExecutionState, validate_execution_transition


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


@dataclass
class CheckpointData:
    """Serializable snapshot of execution state at a given step."""
    task_id: str = ""
    workflow_id: str = ""
    step_id: str = ""
    execution_id: str = field(default_factory=_new_id)
    state: ExecutionState = ExecutionState.RUNNING

    # Progress
    step_index: int = 0
    total_steps: int = 0
    sequence_number: int = 0

    # Execution state
    workflow_state: dict[str, Any] = field(default_factory=dict)
    task_state: dict[str, Any] = field(default_factory=dict)
    intermediate_outputs: dict[str, Any] = field(default_factory=dict)
    memory_references: list[str] = field(default_factory=list)

    # Governance
    governance_metadata: dict[str, Any] = field(default_factory=dict)
    token_id: str | None = None

    # Lease binding
    lease_key: str = ""
    fencing_token: int = 0
    worker_id: str = ""

    # Metadata
    timestamp: str = field(default_factory=_now_iso)
    checkpoint_id: str = field(default_factory=_new_id)
    compressed: bool = False
    payload: bytes | None = None


@dataclass
class ExecutionContext:
    """
    Mutable runtime record for a task execution.

    Drives SM-TASK transitions via .transition(). Re-mints execution_id
    on each retry so stale checkpoints are distinguishable by recovery.
    """
    task_id: str = field(default_factory=_new_id)
    workflow_id: str = ""
    step_id: str = ""
    execution_id: str = field(default_factory=_new_id)

    # SM-TASK state
    state: ExecutionState = ExecutionState.CREATED

    # Task description
    task_type: str = ""
    task_payload: dict[str, Any] = field(default_factory=dict)
    priority: str = "normal"

    # Assignment
    assigned_worker_id: str = ""
    lease_key: str = ""
    fencing_token: int = 0

    # Retry accounting
    attempt: int = 0
    max_attempts: int = 3
    retry_delay_seconds: float = 5.0

    # Lifecycle timestamps (ISO 8601 UTC)
    created_at: str = field(default_factory=_now_iso)
    queued_at: str | None = None
    leased_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None

    # Checkpoint tracking
    last_checkpoint_id: str | None = None
    checkpoint_sequence: int = 0

    # Outcome
    result: dict[str, Any] | None = None
    error: str | None = None

    # Governance / observability
    token_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    def transition(self, to_state: ExecutionState, event: str = "explicit") -> None:
        validate_execution_transition(self.state, to_state, event)
        self.state = to_state
        now = _now_iso()
        if to_state == ExecutionState.QUEUED and not self.queued_at:
            self.queued_at = now
        elif to_state == ExecutionState.LEASED:
            self.leased_at = now
        elif to_state == ExecutionState.RUNNING and not self.started_at:
            self.started_at = now
        elif to_state in (ExecutionState.COMPLETED, ExecutionState.FAILED, ExecutionState.CANCELLED):
            self.completed_at = now

    def can_retry(self) -> bool:
        return self.attempt < self.max_attempts

    def lease_key_for(self) -> str:
        return self.lease_key or f"exec:{self.workflow_id}:{self.step_id}"

    def to_checkpoint(self, step_index: int = 0, total_steps: int = 0) -> CheckpointData:
        return CheckpointData(
            task_id=self.task_id,
            workflow_id=self.workflow_id,
            step_id=self.step_id,
            execution_id=self.execution_id,
            state=self.state,
            step_index=step_index,
            total_steps=total_steps,
            sequence_number=self.checkpoint_sequence,
            token_id=self.token_id,
            lease_key=self.lease_key_for(),
            fencing_token=self.fencing_token,
            worker_id=self.assigned_worker_id,
        )
