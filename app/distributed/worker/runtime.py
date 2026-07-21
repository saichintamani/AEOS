"""
Worker runtime — per-node task execution host.

Receives TASK_ACCEPTED events, queues tasks in a bounded asyncio.Queue,
and dispatches them via registered handlers. Governance is checked before
each execution. A semaphore limits concurrency to max_in_flight.

Architecture Contract: AC-EXEC-001, AC-LIFE-002
Protocol: PROTO-006 (task dispatch)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventConsumer, EventEnvelope, EventPublisher
from app.distributed.execution.checkpoint import CheckpointEngine
from app.distributed.execution.context import ExecutionContext
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.execution.states import ExecutionState
from app.distributed.worker.governance import GovernanceClient, TokenRevokedException
from app.distributed.worker.heartbeat import HeartbeatService

if TYPE_CHECKING:
    from app.security.token_verifier import TokenVerifier

logger = logging.getLogger(__name__)

TaskHandler = Callable[[ExecutionContext, Any], Coroutine[Any, Any, dict]]


@dataclass
class WorkerMetrics:
    in_flight_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    queue_depth: int = 0


class WorkerRuntime:
    """
    Per-node task execution runtime.

    Lifecycle: start() → receives TASK_ACCEPTED events → dispatches handlers
               → publishes TASK_COMPLETED / TASK_FAILED → stop()
    """

    def __init__(
        self,
        identity: NodeIdentity,
        publisher: EventPublisher,
        consumer: EventConsumer,
        lease_manager: ExecutionLeaseManager,
        checkpoint_engine: CheckpointEngine,
        *,
        max_in_flight: int = 16,
        queue_capacity: int = 128,
        heartbeat_interval: float = 10.0,
        token_verifier: "TokenVerifier | None" = None,
        # Fail-closed mode (AC-EXEC-003): when True, tasks without a verifiable
        # signed JWT are rejected before execution (enforced by GovernanceClient).
        # Default False preserves dev/test unauthenticated behavior.
        require_signed_tokens: bool = False,
    ) -> None:
        self._identity = identity
        self._publisher = publisher
        self._consumer = consumer
        self._lease_mgr = lease_manager
        self._cp_engine = checkpoint_engine
        self._max_in_flight = max_in_flight

        self._handlers: dict[str, TaskHandler] = {}
        self._queue: asyncio.Queue[EventEnvelope] = asyncio.Queue(maxsize=queue_capacity)
        self._sem = asyncio.Semaphore(max_in_flight)
        self._running = False
        self._dispatch_task: asyncio.Task | None = None
        self.metrics = WorkerMetrics()

        self._governance = GovernanceClient(
            consumer,
            identity.node_id,
            token_verifier=token_verifier,
            require_signed_tokens=require_signed_tokens,
        )
        self._heartbeat = HeartbeatService(
            publisher=publisher,
            node_id=identity.node_id,
            metrics_provider=lambda: {
                "in_flight_tasks": self.metrics.in_flight_tasks,
                "cpu_utilization": 0.0,
            },
            interval_seconds=heartbeat_interval,
        )

    @property
    def node_id(self) -> str:
        return self._identity.node_id

    @property
    def is_running(self) -> bool:
        return self._running

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self._handlers[task_type] = handler

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._governance.start()
        # Consume TASK_ACCEPTED from the transport so the runtime is driven by
        # real dispatched events (in-process OR over the gRPC event bus), not
        # only by direct _on_task_accepted() calls. The consumer's start()
        # activates every pending subscription (governance topics registered by
        # GovernanceClient.start() above, plus this one) on the transport.
        await self._consumer.subscribe(
            [DistributedEventType.TASK_ACCEPTED],
            self._on_task_accepted,
            self._identity.node_id,
        )
        await self._consumer.start()
        await self._heartbeat.start()
        self._dispatch_task = asyncio.create_task(
            self._dispatch_loop(), name=f"dispatch-{self.node_id}"
        )

    async def stop(self) -> None:
        self._running = False
        await self._consumer.stop()
        await self._governance.stop()
        await self._heartbeat.stop()
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

    # ── Event handler (called by consumer/test) ───────────────────────────────

    async def _on_task_accepted(self, envelope: EventEnvelope) -> None:
        payload = envelope.payload
        if payload.get("assigned_worker_id") != self.node_id:
            return
        try:
            self._queue.put_nowait(envelope)
            self.metrics.queue_depth = self._queue.qsize()
        except asyncio.QueueFull:
            logger.warning("Task queue full on %s — dropping %s", self.node_id, payload.get("task_id"))

    # ── Dispatch loop ─────────────────────────────────────────────────────────

    async def _dispatch_loop(self) -> None:
        while self._running:
            try:
                envelope = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            self.metrics.queue_depth = self._queue.qsize()
            asyncio.create_task(self._execute_with_cleanup(envelope))

    async def _execute_with_cleanup(self, envelope: EventEnvelope) -> None:
        async with self._sem:
            self.metrics.in_flight_tasks += 1
            payload = envelope.payload
            ctx = ExecutionContext(
                task_id=payload.get("task_id", ""),
                workflow_id=payload.get("workflow_id", ""),
                step_id=payload.get("step_id", ""),
                task_type=payload.get("task_type", ""),
                task_payload=payload.get("task_payload", {}),
                priority=payload.get("priority", "normal"),
                assigned_worker_id=self.node_id,
                lease_key=payload.get("lease_key", ""),
                fencing_token=payload.get("fencing_token", 0),
                attempt=payload.get("attempt", 0),
                max_attempts=payload.get("max_attempts", 3),
                token_id=payload.get("token_id"),
                raw_token=payload.get("raw_token"),
                # Worker receives task that scheduler already placed in SCHEDULED state
                state=ExecutionState.SCHEDULED,
            )
            try:
                # Full check: fail-closed mandatory mode, cryptographic JWT
                # verification (when verifier configured), then revocation set.
                await self._governance.verify_token(ctx.token_id, raw_token=ctx.raw_token)
            except TokenRevokedException as exc:
                logger.warning(
                    "Token rejected for task %s (%s) — skipping", ctx.task_id, exc.reason
                )
                self.metrics.failed_tasks += 1
                self.metrics.in_flight_tasks -= 1
                await self._publisher.publish(EventEnvelope(
                    event_type=DistributedEventType.TASK_FAILED,
                    payload={"task_id": ctx.task_id,
                             "error": f"governance token rejected: {exc.reason}"},
                    source_node_id=self.node_id,
                ))
                return

            ctx.transition(ExecutionState.RUNNING)
            handler = self._handlers.get(ctx.task_type)
            try:
                if handler:
                    result = await handler(ctx, None)
                    ctx.result = result
                ctx.transition(ExecutionState.COMPLETED)
                self.metrics.completed_tasks += 1
                await self._publisher.publish(EventEnvelope(
                    event_type=DistributedEventType.TASK_COMPLETED,
                    payload={"task_id": ctx.task_id, "workflow_id": ctx.workflow_id,
                             "result": ctx.result},
                    source_node_id=self.node_id,
                    workflow_id=ctx.workflow_id,
                    task_id=ctx.task_id,
                ))
            except Exception as exc:
                ctx.state = ExecutionState.FAILED
                ctx.error = str(exc)
                self.metrics.failed_tasks += 1
                logger.exception("Task %s failed: %s", ctx.task_id, exc)
                await self._publisher.publish(EventEnvelope(
                    event_type=DistributedEventType.TASK_FAILED,
                    payload={"task_id": ctx.task_id, "error": str(exc)},
                    source_node_id=self.node_id,
                ))
            finally:
                self.metrics.in_flight_tasks -= 1
