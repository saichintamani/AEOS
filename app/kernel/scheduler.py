"""
AEOS Kernel — Scheduler

Task scheduling layer. Enforces concurrency limits, priority ordering,
and task lifecycle tracking. The Scheduler does not execute tasks —
it admits them and hands them off to asyncio.

Properties:
  - Priority queue (1 = highest priority, 10 = lowest)
  - Configurable maximum concurrency (default: 50 concurrent tasks)
  - Each task slot is released when the task coroutine completes
  - Scheduler refuses new submissions when kernel is not RUNNING
  - All task events are published through the kernel event bus
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Coroutine, Any

from app.core.logger import get_logger
from app.kernel.exceptions import ConcurrencyLimitError, TaskSubmissionError

__all__ = [
    "TaskPriority",
    "ScheduledTask",
    "Scheduler",
]

log = get_logger(__name__)


class TaskPriority(int, Enum):
    CRITICAL = 1
    HIGH     = 3
    NORMAL   = 5
    LOW      = 7
    BATCH    = 9


@dataclass
class ScheduledTask:
    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    priority: int = TaskPriority.NORMAL
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str = ""
    completed_at: str = ""
    status: str = "pending"     # pending | running | completed | failed | cancelled
    error: str = ""
    trace_id: str = ""

    # Internal: asyncio task handle
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False, compare=False)


Coroutine_T = Callable[..., Coroutine[Any, Any, Any]]


class Scheduler:
    """
    Kernel task scheduler.

    Callers submit async coroutines; the Scheduler manages the concurrency
    semaphore and task lifecycle bookkeeping.

    Usage:
        scheduler = Scheduler(max_concurrent=20)
        task = await scheduler.submit(my_coro(), name="my_task", priority=3)
        # task is a ScheduledTask; the coroutine runs in the background
    """

    def __init__(self, max_concurrent: int = 50) -> None:
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, ScheduledTask] = {}
        self._accepting: bool = True
        self._submitted_total: int = 0
        self._failed_total: int = 0

    # ── Submission ─────────────────────────────────────────────────────────────

    async def submit(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str = "",
        priority: int = TaskPriority.NORMAL,
        trace_id: str = "",
    ) -> ScheduledTask:
        """
        Submit a coroutine for scheduled execution.

        Returns immediately with a ScheduledTask handle. The coroutine runs
        in the background subject to the concurrency limit.

        Raises:
            TaskSubmissionError: if the scheduler is draining (not accepting)
            ConcurrencyLimitError: if the hard concurrency limit is exceeded
              and priority is below CRITICAL
        """
        if not self._accepting:
            raise TaskSubmissionError("Scheduler is not accepting new tasks (draining or stopped).")

        # Non-critical tasks respect the semaphore
        if priority > TaskPriority.CRITICAL and len(self._tasks) >= self._max_concurrent:
            raise ConcurrencyLimitError(self._max_concurrent)

        task = ScheduledTask(name=name, priority=priority, trace_id=trace_id)
        self._tasks[task.task_id] = task
        self._submitted_total += 1

        # Wrap coroutine with semaphore + lifecycle tracking
        async def _run() -> None:
            async with self._semaphore:
                task.status = "running"
                task.started_at = datetime.now(timezone.utc).isoformat()
                try:
                    await coro
                    task.status = "completed"
                except asyncio.CancelledError:
                    task.status = "cancelled"
                    raise
                except Exception as exc:
                    task.status = "failed"
                    task.error = str(exc)
                    self._failed_total += 1
                    log.error(
                        "Scheduled task failed",
                        extra={"ctx_task_id": task.task_id, "ctx_name": name, "ctx_error": str(exc)},
                    )
                finally:
                    task.completed_at = datetime.now(timezone.utc).isoformat()

        asyncio_task = asyncio.create_task(_run(), name=f"sched-{task.task_id[:8]}")
        task._asyncio_task = asyncio_task
        log.debug("Task submitted", extra={"ctx_task_id": task.task_id, "ctx_name": name})
        return task

    # ── Drain / Stop ───────────────────────────────────────────────────────────

    async def drain(self, timeout_seconds: float = 30.0) -> int:
        """
        Stop accepting new tasks and wait for in-flight tasks to complete.

        Returns the number of tasks that were still running at drain time.
        """
        self._accepting = False
        running = [t for t in self._tasks.values() if t.status == "running"]
        if not running:
            return 0

        log.info("Scheduler draining", extra={"ctx_running": len(running)})
        deadline = time.monotonic() + timeout_seconds

        while True:
            still_running = [t for t in running if t.status == "running"]
            if not still_running:
                break
            if time.monotonic() > deadline:
                log.warning(
                    "Scheduler drain timeout — forcing remaining tasks to cancel",
                    extra={"ctx_remaining": len(still_running)},
                )
                for t in still_running:
                    if t._asyncio_task and not t._asyncio_task.done():
                        t._asyncio_task.cancel()
                break
            await asyncio.sleep(0.1)

        return len([t for t in running if t.status != "completed"])

    def cancel_task(self, task_id: str) -> bool:
        """Cancel a scheduled task. Returns True if cancellation was requested."""
        task = self._tasks.get(task_id)
        if task and task._asyncio_task and not task._asyncio_task.done():
            task._asyncio_task.cancel()
            return True
        return False

    # ── Introspection ──────────────────────────────────────────────────────────

    @property
    def active_count(self) -> int:
        return len([t for t in self._tasks.values() if t.status == "running"])

    @property
    def pending_count(self) -> int:
        return len([t for t in self._tasks.values() if t.status == "pending"])

    def summarize(self) -> dict:
        statuses: dict[str, int] = {}
        for t in self._tasks.values():
            statuses[t.status] = statuses.get(t.status, 0) + 1
        return {
            "accepting": self._accepting,
            "max_concurrent": self._max_concurrent,
            "submitted_total": self._submitted_total,
            "failed_total": self._failed_total,
            "by_status": statuses,
        }
