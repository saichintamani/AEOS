"""
AEOS Distributed Execution Engine — Checkpoint System

Persists execution state after each completed node so that:
  - Executions can resume after a crash
  - Executions can be rolled back to a previous node
  - Partial executions can be replayed from a known-good point

Architecture: ABC + InMemoryCheckpointStore (default).
Future: RedisCheckpointStore, S3CheckpointStore via adapters.

Checkpoints are immutable once saved — a new checkpoint is created
for each state transition, enabling full rollback history.
"""

from __future__ import annotations

import copy
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.logger import get_logger
from app.execution.schemas import StepResult, StepStatus, WorkflowState, WorkflowStatus

__all__ = [
    "Checkpoint",
    "CheckpointStore",
    "InMemoryCheckpointStore",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Checkpoint Dataclass ──────────────────────────────────────────────────────

@dataclass
class Checkpoint:
    """
    Immutable snapshot of a WorkflowState after a node completes.

    step_results is a deep-copy of the live dict so future mutations
    don't corrupt the checkpoint.
    """
    checkpoint_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    trace_id: str = ""
    # The node that just completed to trigger this checkpoint
    trigger_node_id: str = ""
    # Frozen copies of mutable workflow state
    completed_nodes: frozenset[str] = field(default_factory=frozenset)
    failed_nodes: frozenset[str] = field(default_factory=frozenset)
    skipped_nodes: frozenset[str] = field(default_factory=frozenset)
    step_results: dict[str, Any] = field(default_factory=dict)
    revision_count: int = 0
    status: str = WorkflowStatus.EXECUTING.value
    created_at: str = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "workflow_id": self.workflow_id,
            "trace_id": self.trace_id,
            "trigger_node_id": self.trigger_node_id,
            "completed_nodes": list(self.completed_nodes),
            "failed_nodes": list(self.failed_nodes),
            "skipped_nodes": list(self.skipped_nodes),
            "step_results_count": len(self.step_results),
            "revision_count": self.revision_count,
            "status": self.status,
            "created_at": self.created_at,
        }


# ── Abstract Store ─────────────────────────────────────────────────────────────

class CheckpointStore(ABC):
    """
    Interface for checkpoint persistence.

    All methods are async to support remote stores (Redis, S3, DynamoDB).
    """

    @abstractmethod
    async def save(
        self,
        workflow_state: WorkflowState,
        trigger_node_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        """Snapshot workflow_state and persist it. Returns the new Checkpoint."""

    @abstractmethod
    async def load_latest(self, workflow_id: str) -> Checkpoint | None:
        """Return the most recently saved Checkpoint for a workflow."""

    @abstractmethod
    async def load(self, checkpoint_id: str) -> Checkpoint | None:
        """Return a specific Checkpoint by ID."""

    @abstractmethod
    async def list_checkpoints(self, workflow_id: str) -> list[Checkpoint]:
        """Return all checkpoints for a workflow, oldest first."""

    @abstractmethod
    async def restore(
        self,
        checkpoint: Checkpoint,
        target_state: WorkflowState,
    ) -> WorkflowState:
        """
        Apply a checkpoint onto target_state, returning the restored state.

        target_state is modified in-place and returned.
        """

    @abstractmethod
    async def delete(self, checkpoint_id: str) -> bool:
        """Delete a checkpoint. Returns True if it existed."""

    async def rollback_to(
        self,
        checkpoint_id: str,
        target_state: WorkflowState,
    ) -> WorkflowState | None:
        """
        Convenience: load a checkpoint and restore it onto target_state.

        Returns None if checkpoint_id not found.
        """
        checkpoint = await self.load(checkpoint_id)
        if checkpoint is None:
            return None
        return await self.restore(checkpoint, target_state)


# ── In-Memory Implementation ───────────────────────────────────────────────────

class InMemoryCheckpointStore(CheckpointStore):
    """
    Default in-process checkpoint store.

    Checkpoints survive across requests within a process but are lost on restart.
    Suitable for development, testing, and single-process deployments.
    """

    def __init__(self, max_checkpoints_per_workflow: int = 50) -> None:
        # workflow_id → list[Checkpoint] (insertion order = temporal order)
        self._store: dict[str, list[Checkpoint]] = {}
        # checkpoint_id → Checkpoint (index for fast lookup)
        self._index: dict[str, Checkpoint] = {}
        self._max_per_workflow = max_checkpoints_per_workflow

    async def save(
        self,
        workflow_state: WorkflowState,
        trigger_node_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Checkpoint:
        # Deep-copy step_results so mutations don't corrupt the snapshot
        frozen_results: dict[str, Any] = {}
        for node_id, sr in workflow_state.step_results.items():
            if isinstance(sr, StepResult):
                # Serialize to dict for immutability
                frozen_results[node_id] = {
                    "node_id": sr.node_id,
                    "status": sr.status.value,
                    "value": copy.deepcopy(sr.value),
                    "error": sr.error,
                    "agent_id": sr.agent_id,
                    "latency_ms": sr.latency_ms,
                    "confidence": sr.confidence,
                    "produced_at": sr.produced_at,
                }
            else:
                frozen_results[node_id] = copy.deepcopy(sr)

        cp = Checkpoint(
            workflow_id=workflow_state.workflow_id,
            trace_id=workflow_state.trace_id,
            trigger_node_id=trigger_node_id,
            completed_nodes=frozenset(workflow_state.completed_nodes),
            failed_nodes=frozenset(workflow_state.failed_nodes),
            skipped_nodes=frozenset(workflow_state.skipped_nodes),
            step_results=frozen_results,
            revision_count=workflow_state.revision_count,
            status=workflow_state.status.value,
            metadata=dict(metadata or {}),
        )

        wid = workflow_state.workflow_id
        if wid not in self._store:
            self._store[wid] = []

        self._store[wid].append(cp)
        self._index[cp.checkpoint_id] = cp

        # Prune old checkpoints if over limit
        if len(self._store[wid]) > self._max_per_workflow:
            oldest = self._store[wid].pop(0)
            self._index.pop(oldest.checkpoint_id, None)

        log.debug(
            "Checkpoint saved",
            extra={
                "ctx_checkpoint_id": cp.checkpoint_id,
                "ctx_workflow_id": wid,
                "ctx_trigger_node": trigger_node_id,
                "ctx_completed": len(cp.completed_nodes),
            },
        )
        return cp

    async def load_latest(self, workflow_id: str) -> Checkpoint | None:
        checkpoints = self._store.get(workflow_id, [])
        return checkpoints[-1] if checkpoints else None

    async def load(self, checkpoint_id: str) -> Checkpoint | None:
        return self._index.get(checkpoint_id)

    async def list_checkpoints(self, workflow_id: str) -> list[Checkpoint]:
        return list(self._store.get(workflow_id, []))

    async def restore(
        self,
        checkpoint: Checkpoint,
        target_state: WorkflowState,
    ) -> WorkflowState:
        """
        Restore checkpoint state onto target_state.

        Only mutable execution state is restored — graph, task_id, and
        trace_id are preserved from target_state.
        """
        target_state.completed_nodes = set(checkpoint.completed_nodes)
        target_state.failed_nodes = set(checkpoint.failed_nodes)
        target_state.skipped_nodes = set(checkpoint.skipped_nodes)
        target_state.revision_count = checkpoint.revision_count

        # Reconstruct StepResult objects from serialized dicts
        restored_results: dict[str, StepResult] = {}
        for node_id, data in checkpoint.step_results.items():
            if isinstance(data, dict) and "status" in data:
                try:
                    restored_results[node_id] = StepResult(
                        node_id=data["node_id"],
                        status=StepStatus(data["status"]),
                        value=data.get("value"),
                        error=data.get("error", ""),
                        agent_id=data.get("agent_id", ""),
                        latency_ms=data.get("latency_ms", 0.0),
                        confidence=data.get("confidence", 1.0),
                        produced_at=data.get("produced_at", ""),
                    )
                except Exception:
                    restored_results[node_id] = data  # type: ignore[assignment]
            else:
                restored_results[node_id] = data  # type: ignore[assignment]

        target_state.step_results = restored_results

        log.info(
            "Workflow state restored from checkpoint",
            extra={
                "ctx_checkpoint_id": checkpoint.checkpoint_id,
                "ctx_workflow_id": checkpoint.workflow_id,
                "ctx_restored_completed": len(checkpoint.completed_nodes),
            },
        )
        return target_state

    async def delete(self, checkpoint_id: str) -> bool:
        cp = self._index.pop(checkpoint_id, None)
        if cp is None:
            return False
        wid_list = self._store.get(cp.workflow_id, [])
        try:
            wid_list.remove(cp)
        except ValueError:
            pass
        return True

    def checkpoint_count(self, workflow_id: str | None = None) -> int:
        if workflow_id:
            return len(self._store.get(workflow_id, []))
        return sum(len(v) for v in self._store.values())

    def summarize(self) -> dict[str, Any]:
        return {
            "workflows_tracked": len(self._store),
            "total_checkpoints": len(self._index),
            "checkpoints_by_workflow": {
                wid: len(cps) for wid, cps in self._store.items()
            },
        }
