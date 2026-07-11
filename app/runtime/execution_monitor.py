"""
Wave 9B.4 — Execution Monitor

Tracks live execution state for all in-flight tasks.

ExecutionMonitor  — per-coordinator state tracker
TaskState         — live execution state entry
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class LiveState(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    COMPLETED  = "completed"
    FAILED     = "failed"
    RECOVERED  = "recovered"
    CANCELLED  = "cancelled"


@dataclass
class TaskState:
    task_id: str
    workflow_id: str = ""
    worker_id: str = ""
    state: LiveState = LiveState.PENDING
    attempt: int = 0
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    started_at: str = ""
    completed_at: str = ""
    result: Any = None
    error: str = ""


class ExecutionMonitor:
    """
    Thread/coroutine-safe tracker for all live task states.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TaskState] = {}
        self._lock = asyncio.Lock()

    async def track(self, state: TaskState) -> None:
        async with self._lock:
            self._tasks[state.task_id] = state

    async def update(self, task_id: str, **kwargs: Any) -> None:
        async with self._lock:
            entry = self._tasks.get(task_id)
            if entry:
                for k, v in kwargs.items():
                    if hasattr(entry, k):
                        setattr(entry, k, v)

    async def get(self, task_id: str) -> TaskState | None:
        async with self._lock:
            return self._tasks.get(task_id)

    async def all_live(self) -> list[TaskState]:
        async with self._lock:
            return [
                s for s in self._tasks.values()
                if s.state in (LiveState.PENDING, LiveState.RUNNING)
            ]

    async def completed_for_workflow(self, workflow_id: str) -> list[TaskState]:
        async with self._lock:
            return [
                s for s in self._tasks.values()
                if s.workflow_id == workflow_id and s.state == LiveState.COMPLETED
            ]

    async def all_for_workflow(self, workflow_id: str) -> list[TaskState]:
        async with self._lock:
            return [s for s in self._tasks.values() if s.workflow_id == workflow_id]

    async def is_workflow_done(self, workflow_id: str) -> bool:
        async with self._lock:
            tasks = [s for s in self._tasks.values() if s.workflow_id == workflow_id]
            if not tasks:
                return False
            return all(
                s.state in (LiveState.COMPLETED, LiveState.FAILED,
                             LiveState.CANCELLED, LiveState.RECOVERED)
                for s in tasks
            )

    async def workflow_result(self, workflow_id: str) -> dict[str, Any]:
        states = await self.all_for_workflow(workflow_id)
        return {
            "workflow_id": workflow_id,
            "total": len(states),
            "completed": sum(1 for s in states if s.state == LiveState.COMPLETED),
            "failed": sum(1 for s in states if s.state == LiveState.FAILED),
            "results": {s.task_id: s.result for s in states if s.result is not None},
        }
