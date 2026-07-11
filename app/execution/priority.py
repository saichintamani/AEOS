"""
AEOS Distributed Execution Engine — Priority Queue & Deadline Scheduler

Provides:
  - PriorityQueue: min-heap with stable insertion order and deadline awareness
  - DeadlineScheduler: Earliest Deadline First (EDF) with priority tie-breaking
    and starvation prevention via aging

Priority scale: 1 = highest, 10 = lowest (matches kernel Scheduler convention).
"""

from __future__ import annotations

import heapq
import time
from dataclasses import dataclass, field
from typing import Any

from app.core.logger import get_logger
from app.execution.graph import GraphNode

__all__ = [
    "PriorityEntry",
    "PriorityQueue",
    "DeadlineScheduler",
]

log = get_logger(__name__)


@dataclass(order=True)
class PriorityEntry:
    """
    Heap entry. Sort key: (priority, deadline_ms, sequence).
    Lower priority value = higher urgency.
    Earlier deadline = higher urgency within same priority.
    Sequence = FIFO tie-breaker.
    """
    priority: int
    deadline_ms: float
    sequence: int
    node_id: str = field(compare=False)
    enqueued_at: float = field(default_factory=time.monotonic, compare=False)


class PriorityQueue:
    """
    Thread-safe (single-event-loop) priority queue for graph nodes.

    Internally uses a min-heap of PriorityEntry. Supports:
    - O(log n) push/pop
    - O(n) remove by node_id (lazy deletion)
    - Deadline-aware ordering
    """

    def __init__(self) -> None:
        self._heap: list[PriorityEntry] = []
        self._sequence: int = 0
        self._removed: set[str] = set()   # lazy-deleted node ids
        self._count: int = 0               # logical size

    def push(self, node_id: str, priority: int = 5, deadline_ms: float = float("inf")) -> None:
        entry = PriorityEntry(
            priority=priority,
            deadline_ms=deadline_ms,
            sequence=self._sequence,
            node_id=node_id,
        )
        self._sequence += 1
        heapq.heappush(self._heap, entry)
        self._count += 1

    def pop(self) -> str:
        """Pop the highest-priority node. Raises IndexError if empty."""
        self._purge_removed()
        while self._heap:
            entry = heapq.heappop(self._heap)
            if entry.node_id not in self._removed:
                self._count -= 1
                return entry.node_id
        raise IndexError("Priority queue is empty")

    def peek(self) -> str | None:
        """Return the highest-priority node_id without removing it."""
        self._purge_removed()
        for entry in self._heap:
            if entry.node_id not in self._removed:
                return entry.node_id
        return None

    def remove(self, node_id: str) -> bool:
        """
        Lazily remove a node. Returns True if node was present.
        Actual heap entry is cleaned up on next push/pop.
        """
        if node_id in self._removed:
            return False
        # Check if it's in the heap at all (linear scan — acceptable for small queues)
        present = any(e.node_id == node_id for e in self._heap)
        if present:
            self._removed.add(node_id)
            self._count -= 1
            return True
        return False

    def update_priority(self, node_id: str, priority: int, deadline_ms: float = float("inf")) -> bool:
        """
        Change the priority of a queued node.
        Implemented as remove + re-push (standard heap technique).
        """
        if self.remove(node_id):
            self.push(node_id, priority, deadline_ms)
            return True
        return False

    def _purge_removed(self) -> None:
        """Clean up lazily-removed entries from the top of the heap."""
        while self._heap and self._heap[0].node_id in self._removed:
            entry = heapq.heappop(self._heap)
            self._removed.discard(entry.node_id)

    def __len__(self) -> int:
        return max(0, self._count)

    def __bool__(self) -> bool:
        return self._count > 0

    def items(self) -> list[str]:
        """Return all queued node_ids (not in priority order, for inspection)."""
        return [e.node_id for e in self._heap if e.node_id not in self._removed]


class DeadlineScheduler:
    """
    Earliest Deadline First (EDF) scheduler with starvation prevention.

    Scheduling logic:
    1. Nodes with expired deadlines are promoted to CRITICAL priority.
    2. Among remaining nodes, sort by (priority, deadline_ms, enqueued_at).
    3. Nodes waiting longer than starvation_threshold_ms get +1 priority boost.

    Usage:
        scheduler = DeadlineScheduler()
        ordered = scheduler.schedule(ready_nodes)  # returns nodes in dispatch order
    """

    def __init__(self, starvation_threshold_ms: float = 30_000.0) -> None:
        self._starvation_threshold_ms = starvation_threshold_ms
        # node_id → enqueued timestamp (monotonic ms)
        self._enqueued_at: dict[str, float] = {}

    def enqueue(self, node: GraphNode) -> None:
        """Record when a node became ready for scheduling."""
        self._enqueued_at[node.node_id] = time.monotonic() * 1000.0

    def schedule(self, ready_nodes: list[GraphNode]) -> list[GraphNode]:
        """
        Return ready_nodes sorted in dispatch order (highest urgency first).

        Modifies node priority in-place for starvation prevention — this is
        intentional: a node that has been starved should run sooner next cycle.
        """
        if not ready_nodes:
            return []

        now_ms = time.monotonic() * 1000.0

        # Apply starvation boost: nodes waiting too long get priority-1
        for node in ready_nodes:
            enqueued = self._enqueued_at.get(node.node_id, now_ms)
            wait_ms = now_ms - enqueued
            if wait_ms >= self._starvation_threshold_ms and node.priority > 1:
                log.debug(
                    "Priority boost applied (starvation prevention)",
                    extra={
                        "ctx_node_id": node.node_id,
                        "ctx_wait_ms": round(wait_ms, 0),
                        "ctx_old_priority": node.priority,
                    },
                )
                node.priority = max(1, node.priority - 1)

        def sort_key(n: GraphNode) -> tuple[int, float, float]:
            deadline = getattr(n, "timeout_ms", float("inf"))
            enqueued = self._enqueued_at.get(n.node_id, now_ms)
            return (n.priority, deadline, enqueued)

        return sorted(ready_nodes, key=sort_key)

    def dequeue(self, node_id: str) -> None:
        """Remove a node from deadline tracking when it starts executing."""
        self._enqueued_at.pop(node_id, None)

    def waiting_count(self) -> int:
        return len(self._enqueued_at)
