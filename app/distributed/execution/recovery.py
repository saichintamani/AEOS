"""
Recovery runtime — PROTO-009 patterns A, B, C.

Pattern A: No checkpoint available — re-queue for fresh execution.
Pattern B: Uncommitted/stale checkpoint — re-queue, discard stale state.
Pattern C: Committed checkpoint from same execution_id — lease steal, resume.

Contract: AC-EXEC-001
Protocol: PROTO-009
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.distributed.execution.checkpoint import CheckpointData, CheckpointEngine
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager, FencingToken
from app.distributed.execution.states import ExecutionState

logger = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    success: bool
    pattern: str  # "A", "B", or "C"
    checkpoint: CheckpointData | None = None
    token: FencingToken | None = None
    error: str | None = None


class RecoveryRuntime:
    """
    Scans for orphaned executions and applies the correct PROTO-009 recovery pattern.

    Instantiated by each worker that can act as a recovery worker.
    """

    def __init__(
        self,
        lease_manager: ExecutionLeaseManager,
        checkpoint_engine: CheckpointEngine,
        *,
        worker_id: str = "recovery",
    ) -> None:
        self._lease_mgr = lease_manager
        self._cp_engine = checkpoint_engine
        self._worker_id = worker_id

    async def recover(self, ctx: ExecutionContext) -> RecoveryResult:
        """
        Apply PROTO-009 to a context that was in RUNNING/SCHEDULED state
        when its worker crashed.

        Returns RecoveryResult indicating which pattern was applied.
        """
        # Try to load a committed checkpoint
        cp = await self._cp_engine.load(ctx.workflow_id, ctx.step_id)

        if cp is None:
            return await self._pattern_a(ctx)

        if cp.execution_id != ctx.execution_id:
            # Checkpoint belongs to a different execution run — stale
            return await self._pattern_b(ctx, cp)

        # Pattern C: committed checkpoint from the same execution
        return await self._pattern_c(ctx, cp)

    async def _pattern_a(self, ctx: ExecutionContext) -> RecoveryResult:
        """No checkpoint — re-queue for fresh execution."""
        logger.info("PROTO-009 Pattern A: re-queuing %s/%s", ctx.workflow_id, ctx.step_id)
        ctx.attempt += 1
        ctx.state = ExecutionState.QUEUED
        return RecoveryResult(success=True, pattern="A")

    async def _pattern_b(self, ctx: ExecutionContext, cp: CheckpointData) -> RecoveryResult:
        """Stale/uncommitted checkpoint — re-queue."""
        logger.info("PROTO-009 Pattern B: stale checkpoint, re-queuing %s/%s", ctx.workflow_id, ctx.step_id)
        ctx.attempt += 1
        ctx.state = ExecutionState.QUEUED
        return RecoveryResult(success=True, pattern="B", checkpoint=cp)

    async def _pattern_c(self, ctx: ExecutionContext, cp: CheckpointData) -> RecoveryResult:
        """Committed checkpoint — steal lease and resume."""
        logger.info("PROTO-009 Pattern C: resuming %s/%s from step %d",
                    ctx.workflow_id, ctx.step_id, cp.step_index)
        token = await self._lease_mgr.steal(ctx.lease_key_for(), self._worker_id)
        if token is None:
            return RecoveryResult(
                success=False, pattern="C",
                error="Failed to steal lease — another recovery worker may have taken it",
            )
        ctx.state = ExecutionState.RECOVERING
        ctx.assigned_worker_id = self._worker_id
        ctx.fencing_token = token.value
        return RecoveryResult(success=True, pattern="C", checkpoint=cp, token=token)

    async def recover_all_orphans(
        self,
        contexts: list[ExecutionContext],
    ) -> list[RecoveryResult]:
        """Recover all provided orphaned execution contexts."""
        results = []
        for ctx in contexts:
            result = await self.recover(ctx)
            results.append(result)
        return results
