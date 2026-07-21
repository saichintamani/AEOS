"""
SchedulerServiceServicer — task dispatch entry point.

AEOS has no standalone synchronous "task registry" object (dispatch normally
flows through the event bus), so this servicer owns a lightweight in-memory
``_TaskRecord`` registry: enough to schedule, track lifecycle state, cancel,
and stream progress for a task over the wire. That is the real behaviour the
proto contract promises; it is deliberately not a second scheduler engine.

Governance is enforced FAIL-CLOSED when ``require_governance`` is set on the
request (or when the servicer is constructed with ``require_governance=True``):
a task with an absent/expired/revoked/wrong-audience token is rejected with
PERMISSION_DENIED and never enters the registry.

Worker assignment: honour ``task.assigned_worker_id`` if present, else pick a
worker from an injected ``worker_provider()`` (round-robin). With neither, the
task is accepted as PENDING and left unassigned.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable

import grpc

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2 as pb
from aeos.core.v1 import task_pb2_grpc as pb_grpc

from app.security.token_verifier import TokenError, TokenVerifier

from ._util import Broadcaster, now_ts

logger = logging.getLogger(__name__)

WorkerProvider = Callable[[], "list[str]"]


@dataclass
class _TaskRecord:
    task_id: str
    worker_id: str
    status: int
    created_at: object
    updated_at: object
    latest_progress: "pb.TaskProgress | None" = None
    result: "pb.TaskResult | None" = None
    progress_stream: Broadcaster = field(default_factory=Broadcaster)


class SchedulerServiceServicer(pb_grpc.SchedulerServiceServicer):
    def __init__(
        self,
        *,
        verifier: TokenVerifier | None = None,
        require_governance: bool = False,
        worker_provider: WorkerProvider | None = None,
        audience: str = "aeos",
    ) -> None:
        self._verifier = verifier
        self._require_gov = require_governance
        self._worker_provider = worker_provider
        self._audience = audience
        self._tasks: dict[str, _TaskRecord] = {}
        self._lock = asyncio.Lock()
        self._rr = 0

    async def ScheduleTask(self, request, context):  # noqa: N802
        task = request.task
        if not task.task_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "task_id is required")

        if request.require_governance or self._require_gov:
            reason = self._check_governance(task.governance_token)
            if reason is not None:
                # Fail-closed: no token / bad token → task never scheduled.
                logger.warning("ScheduleTask rejected task=%s: %s", task.task_id, reason)
                await context.abort(grpc.StatusCode.PERMISSION_DENIED, reason)

        worker_id = await self.admit(task)
        return pb.ScheduleTaskResponse(
            task_id=task.task_id, assigned_worker_id=worker_id, queue_position=0,
        )

    async def admit(self, task: "pb.Task") -> str:
        """Register a task in the scheduler and return its assigned worker id.

        The direct (non-gRPC) admission path — used by ScheduleTask after
        governance passes, and by the federation layer to route an already-
        session-authorized federated task into this cluster's registry.
        """
        worker_id = task.assigned_worker_id or self._pick_worker()
        status = pb.TASK_STATUS_ASSIGNED if worker_id else pb.TASK_STATUS_PENDING
        ts = now_ts()
        async with self._lock:
            self._tasks[task.task_id] = _TaskRecord(
                task_id=task.task_id, worker_id=worker_id, status=status,
                created_at=ts, updated_at=ts,
            )
        return worker_id

    async def CancelTask(self, request, context):  # noqa: N802
        async with self._lock:
            rec = self._tasks.get(request.task_id)
            if rec is None:
                return pb.CancelTaskResponse(task_id=request.task_id, cancelled=False)
            prev = rec.status
            terminal = (pb.TASK_STATUS_SUCCEEDED, pb.TASK_STATUS_FAILED,
                        pb.TASK_STATUS_CANCELLED, pb.TASK_STATUS_TIMED_OUT)
            if prev in terminal:
                return pb.CancelTaskResponse(
                    task_id=request.task_id, prev_status=prev, cancelled=False)
            if prev == pb.TASK_STATUS_RUNNING and not request.force:
                return pb.CancelTaskResponse(
                    task_id=request.task_id, prev_status=prev, cancelled=False)
            rec.status = pb.TASK_STATUS_CANCELLED
            rec.updated_at = now_ts()
        return pb.CancelTaskResponse(
            task_id=request.task_id, prev_status=prev, cancelled=True)

    async def GetTaskStatus(self, request, context):  # noqa: N802
        async with self._lock:
            rec = self._tasks.get(request.task_id)
            if rec is None:
                await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task {request.task_id}")
            resp = pb.GetTaskStatusResponse(
                task_id=rec.task_id, status=rec.status, worker_id=rec.worker_id,
            )
            resp.created_at.CopyFrom(rec.created_at)
            resp.updated_at.CopyFrom(rec.updated_at)
            if rec.latest_progress is not None:
                resp.latest_progress.CopyFrom(rec.latest_progress)
            if rec.result is not None:
                resp.result.CopyFrom(rec.result)
        return resp

    async def WatchTask(self, request, context):  # noqa: N802
        async with self._lock:
            rec = self._tasks.get(request.task_id)
        if rec is None:
            await context.abort(grpc.StatusCode.NOT_FOUND, f"unknown task {request.task_id}")
        q = await rec.progress_stream.subscribe()
        try:
            if rec.latest_progress is not None:
                yield rec.latest_progress
            while True:
                yield await q.get()
        finally:
            await rec.progress_stream.unsubscribe(q)

    # ── ingest hooks (used by WorkerService / tests to advance a task) ─────────
    async def report_progress(self, progress: "pb.TaskProgress") -> None:
        async with self._lock:
            rec = self._tasks.get(progress.task_id)
            if rec is None:
                return
            rec.latest_progress = progress
            rec.status = pb.TASK_STATUS_RUNNING
            rec.updated_at = now_ts()
        await rec.progress_stream.publish(progress)

    async def report_result(self, result: "pb.TaskResult") -> None:
        async with self._lock:
            rec = self._tasks.get(result.task_id)
            if rec is None:
                return
            rec.result = result
            rec.status = result.status
            rec.updated_at = now_ts()

    # ── internals ─────────────────────────────────────────────────────────────
    def _check_governance(self, token: str) -> str | None:
        """Return None if the token is valid, else a human reason (fail-closed)."""
        if not token:
            return "governance_token missing"
        if self._verifier is None:
            return "governance required but no verifier configured"
        try:
            self._verifier.verify(token, audience=self._audience)
        except TokenError as exc:
            return f"governance token rejected: {exc}"
        except Exception as exc:  # pragma: no cover - defensive
            return f"governance verify error: {exc}"
        return None

    def _pick_worker(self) -> str:
        if self._worker_provider is None:
            return ""
        workers = self._worker_provider()
        if not workers:
            return ""
        self._rr = (self._rr + 1) % len(workers)
        return workers[self._rr]
