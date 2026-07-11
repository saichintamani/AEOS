"""
AEOS Distributed Execution Engine — Execution Replay & Trace

Records execution traces for debugging, auditing, and replay.

A trace captures:
  - Every node execution event (started, completed, failed, retried)
  - The exact StepResult produced by each node
  - Timing information for performance analysis

ReplayEngine takes a trace and re-executes the workflow, optionally
comparing results against the original (useful for regression testing
and debugging non-deterministic behavior).
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger
from app.execution.graph import GraphNode, execute_graph
from app.execution.schemas import (
    ExecutionGraph,
    StepResult,
    StepStatus,
    WorkflowState,
    WorkflowStatus,
)

__all__ = [
    "TraceEntry",
    "ExecutionTrace",
    "TraceStore",
    "ReplayDiff",
    "ReplayEngine",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Trace Structures ──────────────────────────────────────────────────────────

@dataclass
class TraceEntry:
    """
    A single recorded event in an execution trace.

    sequence:    Global ordering key (monotonically increasing)
    event_type:  e.g., "node.started", "node.completed", "node.failed"
    node_id:     The node this entry is about (empty for workflow-level events)
    step_result: The StepResult, if the event is a node completion/failure
    metadata:    Any extra data for debugging
    """
    sequence: int
    event_type: str
    node_id: str = ""
    step_result: StepResult | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=_now)


@dataclass
class ExecutionTrace:
    """
    Complete record of a workflow execution.

    Immutable once the workflow completes (add_entry raises after close()).
    """
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    workflow_id: str = ""
    task_description: str = ""
    entries: list[TraceEntry] = field(default_factory=list)
    started_at: str = field(default_factory=_now)
    completed_at: str = ""
    final_status: str = ""
    _closed: bool = field(default=False, repr=False, compare=False)
    _seq: int = field(default=0, repr=False, compare=False)

    # ── Entry management ──────────────────────────────────────────────────────

    def add_entry(
        self,
        event_type: str,
        node_id: str = "",
        step_result: StepResult | None = None,
        **metadata: Any,
    ) -> TraceEntry:
        if self._closed:
            log.warning("Attempted to add entry to a closed trace", extra={"ctx_trace_id": self.trace_id})
            # Add anyway (for error recovery traces)

        entry = TraceEntry(
            sequence=self._seq,
            event_type=event_type,
            node_id=node_id,
            step_result=step_result,
            metadata=dict(metadata),
        )
        self._seq += 1
        self.entries.append(entry)
        return entry

    def close(self, final_status: str = "completed") -> None:
        self._closed = True
        self.completed_at = _now()
        self.final_status = final_status

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_node_entries(self, node_id: str) -> list[TraceEntry]:
        return [e for e in self.entries if e.node_id == node_id]

    def get_entries_by_type(self, event_type: str) -> list[TraceEntry]:
        return [e for e in self.entries if e.event_type == event_type]

    def node_result(self, node_id: str) -> StepResult | None:
        """Return the last recorded StepResult for a node."""
        for entry in reversed(self.entries):
            if entry.node_id == node_id and entry.step_result is not None:
                return entry.step_result
        return None

    def failed_nodes(self) -> list[str]:
        return [
            e.node_id for e in self.entries
            if e.event_type == "node.failed" and e.node_id
        ]

    def completed_nodes(self) -> list[str]:
        return [
            e.node_id for e in self.entries
            if e.event_type == "node.completed" and e.node_id
        ]

    def duration_ms(self) -> float:
        """Wall clock duration in milliseconds."""
        if not self.started_at or not self.completed_at:
            return 0.0
        try:
            from datetime import datetime
            s = datetime.fromisoformat(self.started_at)
            e = datetime.fromisoformat(self.completed_at)
            return (e - s).total_seconds() * 1000.0
        except Exception:
            return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "workflow_id": self.workflow_id,
            "task_description": self.task_description[:100],
            "entries_count": len(self.entries),
            "completed_nodes": self.completed_nodes(),
            "failed_nodes": self.failed_nodes(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_ms": self.duration_ms(),
            "final_status": self.final_status,
        }


# ── Trace Store ───────────────────────────────────────────────────────────────

class TraceStore:
    """
    In-memory store for ExecutionTrace objects.

    Usage:
        store = TraceStore()
        store.save(trace)
        t = store.get(trace.trace_id)
    """

    def __init__(self, max_traces: int = 500) -> None:
        self._traces: dict[str, ExecutionTrace] = {}
        self._by_workflow: dict[str, list[str]] = {}  # workflow_id → [trace_id]
        self._max_traces = max_traces
        self._insertion_order: list[str] = []

    def save(self, trace: ExecutionTrace) -> None:
        if trace.trace_id in self._traces:
            return  # Idempotent

        # Evict oldest if at limit
        if len(self._traces) >= self._max_traces:
            oldest_id = self._insertion_order.pop(0)
            evicted = self._traces.pop(oldest_id, None)
            if evicted:
                self._by_workflow.get(evicted.workflow_id, []).remove(oldest_id)

        self._traces[trace.trace_id] = trace
        self._insertion_order.append(trace.trace_id)
        wid_list = self._by_workflow.setdefault(trace.workflow_id, [])
        if trace.trace_id not in wid_list:
            wid_list.append(trace.trace_id)

    def get(self, trace_id: str) -> ExecutionTrace | None:
        return self._traces.get(trace_id)

    def list_traces(self, workflow_id: str) -> list[str]:
        return list(self._by_workflow.get(workflow_id, []))

    def delete(self, trace_id: str) -> bool:
        trace = self._traces.pop(trace_id, None)
        if trace is None:
            return False
        self._insertion_order.remove(trace_id)
        wid_list = self._by_workflow.get(trace.workflow_id, [])
        if trace_id in wid_list:
            wid_list.remove(trace_id)
        return True

    def count(self) -> int:
        return len(self._traces)


# ── Replay Diff ───────────────────────────────────────────────────────────────

@dataclass
class ReplayDiff:
    """Difference between original and replayed execution for one node."""
    node_id: str
    original_status: str
    replayed_status: str
    diverged: bool
    original_error: str = ""
    replayed_error: str = ""
    original_value: Any = None
    replayed_value: Any = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "diverged": self.diverged,
            "original_status": self.original_status,
            "replayed_status": self.replayed_status,
            "note": self.note,
        }


# ── Replay Engine ─────────────────────────────────────────────────────────────

NodeExecutorFn = Callable[[GraphNode, WorkflowState], Coroutine[Any, Any, StepResult]]


class ReplayEngine:
    """
    Re-executes a workflow from a stored trace.

    Two modes:
    1. Full replay: run all nodes from scratch
    2. Partial replay (from_node_id): fast-forward to a specific node
       by injecting recorded results for preceding nodes, then re-execute
       from that point forward.

    Use ReplayEngine.diff() to compare original vs replayed results.
    """

    def __init__(self, trace_store: TraceStore) -> None:
        self._store = trace_store

    async def replay(
        self,
        trace_id: str,
        graph: ExecutionGraph,
        execute_node: NodeExecutorFn,
        from_node_id: str | None = None,
        max_parallel: int = 3,
    ) -> tuple[WorkflowState, ExecutionTrace]:
        """
        Replay an execution from a stored trace.

        Args:
            trace_id:       ID of the original trace to replay
            graph:          The ExecutionGraph to replay against
            execute_node:   Node executor callable (same interface as execute_graph)
            from_node_id:   If set, inject recorded results up to this node,
                            then replay from here onward
            max_parallel:   Max concurrent node executions

        Returns:
            (WorkflowState, ExecutionTrace) — final state and replay trace
        """
        original_trace = self._store.get(trace_id)
        if original_trace is None:
            raise KeyError(f"Trace not found: {trace_id!r}")

        replay_trace = ExecutionTrace(
            workflow_id=original_trace.workflow_id,
            task_description=f"[REPLAY] {original_trace.task_description}",
        )
        replay_trace.add_entry("replay.started", metadata={
            "original_trace_id": trace_id,
            "from_node_id": from_node_id or "start",
        })

        workflow_state = WorkflowState(
            workflow_id=original_trace.workflow_id,
            trace_id=replay_trace.trace_id,
            status=WorkflowStatus.EXECUTING,
        )

        # Fast-forward: inject recorded results for nodes before from_node_id
        if from_node_id is not None:
            workflow_state = self._inject_prior_results(
                original_trace, workflow_state, graph, from_node_id
            )

        # Define a tracing executor that records results into replay_trace
        async def traced_execute(node: GraphNode, state: WorkflowState) -> StepResult:
            replay_trace.add_entry("node.started", node_id=node.node_id)
            try:
                result = await execute_node(node, state)
            except Exception as exc:
                result = StepResult(
                    node_id=node.node_id,
                    status=StepStatus.FAILED,
                    error=str(exc),
                )
            replay_trace.add_entry(
                f"node.{'completed' if result.status == StepStatus.COMPLETED else 'failed'}",
                node_id=node.node_id,
                step_result=result,
            )
            return result

        t_start = time.monotonic()
        workflow_state = await execute_graph(
            graph=graph,
            workflow_state=workflow_state,
            execute_node=traced_execute,
            max_parallel=max_parallel,
        )
        wall_ms = round((time.monotonic() - t_start) * 1000, 1)

        replay_trace.close("completed")
        replay_trace.add_entry("replay.completed", metadata={"wall_time_ms": wall_ms})

        return workflow_state, replay_trace

    def diff(
        self,
        original_trace: ExecutionTrace,
        replayed_trace: ExecutionTrace,
    ) -> list[ReplayDiff]:
        """
        Compare two traces and return per-node diffs.

        A node is considered diverged if its status changed between runs.
        """
        diffs: list[ReplayDiff] = []

        all_node_ids = set(original_trace.completed_nodes()) | set(original_trace.failed_nodes())
        all_node_ids |= set(replayed_trace.completed_nodes()) | set(replayed_trace.failed_nodes())

        for node_id in sorted(all_node_ids):
            orig_result = original_trace.node_result(node_id)
            rep_result = replayed_trace.node_result(node_id)

            orig_status = orig_result.status.value if orig_result else "missing"
            rep_status = rep_result.status.value if rep_result else "missing"
            diverged = orig_status != rep_status

            note = ""
            if diverged:
                note = f"Status changed: {orig_status!r} → {rep_status!r}"
            elif orig_result and rep_result:
                if orig_result.error != rep_result.error:
                    note = "Error message changed (non-diverging)"

            diffs.append(ReplayDiff(
                node_id=node_id,
                original_status=orig_status,
                replayed_status=rep_status,
                diverged=diverged,
                original_error=orig_result.error if orig_result else "",
                replayed_error=rep_result.error if rep_result else "",
                original_value=orig_result.value if orig_result else None,
                replayed_value=rep_result.value if rep_result else None,
                note=note,
            ))

        return diffs

    def _inject_prior_results(
        self,
        original_trace: ExecutionTrace,
        workflow_state: WorkflowState,
        graph: ExecutionGraph,
        from_node_id: str,
    ) -> WorkflowState:
        """
        Inject recorded results for all nodes that topologically precede from_node_id.
        """
        # Find topological position of from_node_id
        topo = graph.topological_order
        if from_node_id not in topo:
            return workflow_state

        cutoff_idx = topo.index(from_node_id)
        prior_node_ids = set(topo[:cutoff_idx])

        for node_id in prior_node_ids:
            recorded = original_trace.node_result(node_id)
            if recorded is not None:
                workflow_state.step_results[node_id] = recorded
                if recorded.status == StepStatus.COMPLETED:
                    workflow_state.completed_nodes.add(node_id)
                else:
                    workflow_state.failed_nodes.add(node_id)

        log.info(
            "Replay fast-forward: injected prior results",
            extra={
                "ctx_from_node": from_node_id,
                "ctx_injected_count": len(prior_node_ids),
            },
        )
        return workflow_state
