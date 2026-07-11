"""
AEOS Distributed Execution Engine — Worker Pool

Provides a pool of async workers for node execution.
Workers acquire a semaphore slot, execute a node, release the slot.

This decouples concurrency control from the execution graph logic.
Future: WorkerPool can be backed by Celery/Redis/SQS workers via adapters.

Architecture:
  WorkerPool
    ├── Semaphore (concurrency control)
    ├── Worker[] (stateful execution slots)
    └── MetricsCollector (optional metrics integration)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger
from app.execution.graph import GraphNode
from app.execution.schemas import StepResult, StepStatus, WorkflowState

__all__ = [
    "WorkerStatus",
    "WorkerStats",
    "Worker",
    "WorkerPool",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerStatus(str, Enum):
    IDLE     = "idle"
    BUSY     = "busy"
    DRAINING = "draining"
    STOPPED  = "stopped"


@dataclass
class WorkerStats:
    tasks_completed: int = 0
    tasks_failed: int = 0
    tasks_timed_out: int = 0
    total_latency_ms: float = 0.0
    current_node_id: str = ""
    last_completed_at: str = ""

    @property
    def total_tasks(self) -> int:
        return self.tasks_completed + self.tasks_failed + self.tasks_timed_out

    @property
    def success_rate(self) -> float:
        return self.tasks_completed / max(self.total_tasks, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.total_tasks, 1)


NodeExecutorCallable = Callable[[GraphNode, WorkflowState], Coroutine[Any, Any, StepResult]]


class Worker:
    """
    A single async execution slot.

    Workers are not pre-started — they execute on demand within the pool's
    semaphore. A Worker tracks its own stats independently.
    """

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.status = WorkerStatus.IDLE
        self.stats = WorkerStats()

    async def execute(
        self,
        node: GraphNode,
        state: WorkflowState,
        executor_fn: NodeExecutorCallable,
    ) -> StepResult:
        self.status = WorkerStatus.BUSY
        self.stats.current_node_id = node.node_id
        t_start = time.time()

        try:
            result = await executor_fn(node, state)
        except asyncio.CancelledError:
            self.status = WorkerStatus.IDLE
            self.stats.current_node_id = ""
            raise
        except Exception as exc:
            latency_ms = round((time.time() - t_start) * 1000, 1)
            result = StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=str(exc),
                latency_ms=latency_ms,
            )

        latency_ms = result.latency_ms or round((time.time() - t_start) * 1000, 1)
        self.stats.total_latency_ms += latency_ms
        self.stats.last_completed_at = _now()
        self.stats.current_node_id = ""
        self.status = WorkerStatus.IDLE

        if result.status == StepStatus.COMPLETED:
            self.stats.tasks_completed += 1
        elif result.status == StepStatus.TIMED_OUT:
            self.stats.tasks_timed_out += 1
        else:
            self.stats.tasks_failed += 1

        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "status": self.status.value,
            "tasks_completed": self.stats.tasks_completed,
            "tasks_failed": self.stats.tasks_failed,
            "success_rate": round(self.stats.success_rate, 4),
            "avg_latency_ms": round(self.stats.avg_latency_ms, 1),
            "current_node_id": self.stats.current_node_id,
        }


class WorkerPool:
    """
    Async worker pool with configurable concurrency.

    Workers are logical slots backed by a semaphore — not OS threads.
    Each submitted task acquires a semaphore slot before executing.

    Usage:
        pool = WorkerPool(size=4, executor_fn=my_executor)
        result = await pool.submit(node, workflow_state)
        await pool.drain(timeout_seconds=30.0)
    """

    def __init__(
        self,
        size: int = 4,
        executor_fn: NodeExecutorCallable | None = None,
    ) -> None:
        self._size = max(1, size)
        self._semaphore = asyncio.Semaphore(self._size)
        self._executor_fn = executor_fn
        self._workers: list[Worker] = [
            Worker(worker_id=f"worker-{i:02d}") for i in range(self._size)
        ]
        self._active_tasks: dict[str, asyncio.Task] = {}  # node_id → asyncio.Task
        self._total_submitted: int = 0
        self._accepting: bool = True

    def set_executor(self, executor_fn: NodeExecutorCallable) -> None:
        """Attach an executor function after pool creation."""
        self._executor_fn = executor_fn

    async def submit(
        self,
        node: GraphNode,
        state: WorkflowState,
        executor_fn: NodeExecutorCallable | None = None,
    ) -> StepResult:
        """
        Submit a node for execution. Blocks until a worker slot is free.

        Args:
            node:           The graph node to execute
            state:          Current workflow state
            executor_fn:    Optional per-call executor override

        Returns:
            StepResult from the worker
        """
        if not self._accepting:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error="WorkerPool is draining — not accepting new submissions",
            )

        fn = executor_fn or self._executor_fn
        if fn is None:
            raise RuntimeError("WorkerPool has no executor_fn configured")

        self._total_submitted += 1
        async with self._semaphore:
            worker = self._get_idle_worker()
            return await worker.execute(node, state, fn)

    def submit_nowait(
        self,
        node: GraphNode,
        state: WorkflowState,
        executor_fn: NodeExecutorCallable | None = None,
    ) -> asyncio.Task:
        """
        Non-blocking submit — returns an asyncio.Task immediately.

        The task is tracked and can be awaited or cancelled.
        """
        fn = executor_fn or self._executor_fn
        if fn is None:
            raise RuntimeError("WorkerPool has no executor_fn configured")
        self._total_submitted += 1
        task = asyncio.create_task(
            self.submit(node, state, fn),
            name=f"worker-pool-{node.node_id[:8]}",
        )
        self._active_tasks[node.node_id] = task
        task.add_done_callback(lambda t: self._active_tasks.pop(node.node_id, None))
        return task

    async def drain(self, timeout_seconds: float = 30.0) -> int:
        """
        Stop accepting new tasks and wait for in-flight tasks to finish.

        Returns the number of tasks that were still active at drain time.
        """
        self._accepting = False
        if not self._active_tasks:
            return 0

        active = list(self._active_tasks.values())
        log.info("WorkerPool draining", extra={"ctx_active": len(active)})

        try:
            done, pending = await asyncio.wait(active, timeout=timeout_seconds)
        except Exception:
            pending = set(active)

        for task in pending:
            task.cancel()

        remaining = len(pending)
        if remaining:
            log.warning(
                "WorkerPool drain timeout — cancelled remaining tasks",
                extra={"ctx_remaining": remaining},
            )
        return remaining

    def cancel_node(self, node_id: str) -> bool:
        """Cancel an in-flight node task. Returns True if found."""
        task = self._active_tasks.get(node_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def _get_idle_worker(self) -> Worker:
        """Return the first idle worker; if none, pick the least-busy one."""
        for w in self._workers:
            if w.status == WorkerStatus.IDLE:
                return w
        # All busy (shouldn't happen with proper semaphore, but be safe)
        return min(self._workers, key=lambda w: w.stats.total_tasks)

    @property
    def active_count(self) -> int:
        return len(self._active_tasks)

    @property
    def available_slots(self) -> int:
        return self._size - self.active_count

    def summarize(self) -> dict[str, Any]:
        return {
            "pool_size": self._size,
            "accepting": self._accepting,
            "total_submitted": self._total_submitted,
            "active_tasks": self.active_count,
            "workers": [w.to_dict() for w in self._workers],
        }
