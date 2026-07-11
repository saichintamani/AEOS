"""
Distributed scheduler.

Selects a worker via the active strategy, acquires an execution lease,
transitions the context QUEUED → LEASED, and publishes a TASK_ACCEPTED
event to the worker's execution topic.

Contract: AC-SCHED-001
"""

from __future__ import annotations

import logging

from app.distributed.contracts.events import DistributedEventType, EventEnvelope, EventPublisher
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.execution.states import ExecutionState
from app.distributed.scheduler.contracts import (
    SchedulingDecision,
    SchedulingError,
    SchedulingRequest,
    SchedulingStrategy,
    WorkerView,
)

logger = logging.getLogger(__name__)


class DistributedScheduler:
    """
    Top-level task scheduler.

    Usage:
        decision = await scheduler.schedule(ctx, worker_views)
    """

    def __init__(
        self,
        strategy: SchedulingStrategy,
        lease_manager: ExecutionLeaseManager,
        publisher: EventPublisher,
        *,
        scheduler_node_id: str = "scheduler",
        default_ttl_seconds: int = 120,
    ) -> None:
        self._strategy = strategy
        self._lease_mgr = lease_manager
        self._publisher = publisher
        self._node_id = scheduler_node_id
        self._ttl = default_ttl_seconds
        self._scheduled_count = 0

    def set_strategy(self, strategy: SchedulingStrategy) -> None:
        self._strategy = strategy

    @property
    def scheduled_count(self) -> int:
        return self._scheduled_count

    async def schedule(
        self,
        ctx: ExecutionContext,
        workers: list[WorkerView],
    ) -> SchedulingDecision:
        request = SchedulingRequest(
            task_id=ctx.task_id,
            workflow_id=ctx.workflow_id,
            step_id=ctx.step_id,
            task_type=ctx.task_type,
            priority=ctx.priority,
        )
        decision = self._strategy.select(workers, request)
        if decision is None:
            raise SchedulingError(
                f"No healthy worker available for task {ctx.task_id!r} "
                f"(strategy={self._strategy.name!r})"
            )

        lease_key = ctx.lease_key_for()
        token = await self._lease_mgr.acquire(lease_key, decision.worker.node_id, ttl_seconds=self._ttl)
        if token is None:
            raise SchedulingError(
                f"Failed to acquire lease {lease_key!r} for worker {decision.worker.node_id!r} "
                "(contention)"
            )

        ctx.lease_key = lease_key
        ctx.fencing_token = token.value
        ctx.assigned_worker_id = decision.worker.node_id
        ctx.transition(ExecutionState.LEASED)

        await self._publisher.publish(EventEnvelope(
            event_type=DistributedEventType.TASK_ACCEPTED,
            payload={
                "task_id": ctx.task_id,
                "workflow_id": ctx.workflow_id,
                "step_id": ctx.step_id,
                "task_type": ctx.task_type,
                "task_payload": ctx.task_payload,
                "priority": ctx.priority,
                "lease_key": lease_key,
                "fencing_token": token.value,
                "assigned_worker_id": decision.worker.node_id,
                "attempt": ctx.attempt,
                "max_attempts": ctx.max_attempts,
            },
            source_node_id=self._node_id,
            workflow_id=ctx.workflow_id,
            task_id=ctx.task_id,
        ))

        self._scheduled_count += 1
        logger.info(
            "Scheduled %s → %s (strategy=%s, token=%d)",
            ctx.task_id, decision.worker.node_id, self._strategy.name, token.value,
        )
        return decision

    async def cancel(self, ctx: ExecutionContext) -> None:
        """Cancel a scheduled task: release lease, publish TASK_CANCELLED."""
        if ctx.lease_key and ctx.fencing_token:
            from app.distributed.execution.lease import FencingToken
            token = FencingToken(
                lease_key=ctx.lease_key,
                value=ctx.fencing_token,
                holder_id=ctx.assigned_worker_id,
            )
            try:
                await self._lease_mgr.release(token)
            except Exception:
                pass

        ctx.transition(ExecutionState.CANCELLED)
        await self._publisher.publish(EventEnvelope(
            event_type=DistributedEventType.TASK_CANCELLED,
            payload={"task_id": ctx.task_id, "workflow_id": ctx.workflow_id},
            source_node_id=self._node_id,
            workflow_id=ctx.workflow_id,
            task_id=ctx.task_id,
        ))
