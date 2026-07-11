"""
Task execution engine.

Drives a single task through LEASED → SCHEDULED → RUNNING → COMPLETED.
A concurrent lease-verifier loop detects lease theft and raises
StaleFencingTokenError to abort the execution (INV-EXEC-001).

ExecutionCallbacks is injected into task handlers for checkpoint/progress/verify.

Contract: AC-EXEC-001
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from app.distributed.execution.checkpoint import CheckpointData, CheckpointEngine
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager, FencingToken, StaleFencingTokenError
from app.distributed.execution.states import ExecutionState

logger = logging.getLogger(__name__)

TaskHandler = Callable[["ExecutionContext", "ExecutionCallbacks"], Coroutine[Any, Any, dict]]


@dataclass
class ExecutionCallbacks:
    """Injected into handlers so tasks can checkpoint, report progress, and verify lease."""
    _engine: "TaskExecutionEngine"
    _ctx: ExecutionContext
    _token: FencingToken

    async def checkpoint(self, step_index: int = 0, total_steps: int = 0, **state) -> None:
        """Write and commit a two-phase checkpoint."""
        data = self._ctx.to_checkpoint(step_index, total_steps)
        data.task_state.update(state)
        entry = await self._engine._cp_engine.write_full(data)
        await self._engine._cp_engine.commit(entry)
        self._ctx.last_checkpoint_id = entry.checkpoint_id
        self._ctx.checkpoint_sequence += 1

    async def verify_lease(self) -> None:
        """Raise StaleFencingTokenError if this task's lease was stolen."""
        valid = await self._engine._lease_mgr.verify(self._token)
        if not valid:
            raise StaleFencingTokenError(
                self._token.lease_key, self._token.value, self._token.value + 1
            )

    async def progress(self, pct: float, message: str = "") -> None:
        self._ctx.metrics["progress"] = pct
        if message:
            self._ctx.metrics["progress_message"] = message


@dataclass
class ExecutionResult:
    success: bool
    ctx: ExecutionContext
    result: dict | None = None
    error: str | None = None


class TaskExecutionEngine:
    """
    Orchestrates a single task execution lifecycle.

    Callers:
      1. await engine.execute(ctx) — runs the full lifecycle
      2. The handler is looked up by ctx.task_type
      3. A background lease verifier runs concurrently; raises on theft
    """

    def __init__(
        self,
        lease_manager: ExecutionLeaseManager,
        checkpoint_engine: CheckpointEngine,
        *,
        lease_verify_interval: float = 10.0,
    ) -> None:
        self._lease_mgr = lease_manager
        self._cp_engine = checkpoint_engine
        self._verify_interval = lease_verify_interval
        self._handlers: dict[str, TaskHandler] = {}

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        handler = self._handlers.get(ctx.task_type)
        if handler is None:
            ctx.state = ExecutionState.FAILED
            ctx.error = f"No handler registered for task_type={ctx.task_type!r}"
            return ExecutionResult(success=False, ctx=ctx, error=ctx.error)

        # Acquire lease
        token = await self._lease_mgr.acquire(ctx.lease_key_for(), ctx.assigned_worker_id)
        if token is None:
            ctx.state = ExecutionState.FAILED
            ctx.error = "Failed to acquire execution lease"
            return ExecutionResult(success=False, ctx=ctx, error=ctx.error)

        ctx.fencing_token = token.value
        ctx.transition(ExecutionState.SCHEDULED)
        ctx.transition(ExecutionState.RUNNING)

        callbacks = ExecutionCallbacks(_engine=self, _ctx=ctx, _token=token)

        # Run handler and lease verifier concurrently
        handler_task = asyncio.create_task(handler(ctx, callbacks))
        verifier_task = asyncio.create_task(self._lease_verifier_loop(token, handler_task))

        try:
            result = await handler_task
            ctx.result = result
            ctx.transition(ExecutionState.COMPLETED)
            return ExecutionResult(success=True, ctx=ctx, result=result)
        except StaleFencingTokenError as exc:
            ctx.state = ExecutionState.FAILED
            ctx.error = str(exc)
            logger.warning("Stale token detected during execution: %s", exc)
            return ExecutionResult(success=False, ctx=ctx, error=ctx.error)
        except Exception as exc:
            ctx.state = ExecutionState.FAILED
            ctx.error = str(exc)
            logger.exception("Task execution failed: %s", exc)
            return ExecutionResult(success=False, ctx=ctx, error=ctx.error)
        finally:
            verifier_task.cancel()
            try:
                await verifier_task
            except asyncio.CancelledError:
                pass
            try:
                await self._lease_mgr.release(token)
            except StaleFencingTokenError:
                pass  # already stolen — that's expected on lease theft

    async def _lease_verifier_loop(
        self,
        token: FencingToken,
        guarded_task: asyncio.Task,
    ) -> None:
        while not guarded_task.done():
            await asyncio.sleep(self._verify_interval)
            if guarded_task.done():
                break
            valid = await self._lease_mgr.verify(token)
            if not valid:
                guarded_task.cancel()
                break
