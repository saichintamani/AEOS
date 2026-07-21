"""
WorkerServiceServicer — worker fleet registration & health, hosted by the
scheduler; workers connect to it.

Delegates to the real ``WorkerPool`` (app/distributed/pool/worker_pool.py):

  - RegisterWorker  → WorkerPool.register(WorkerSnapshot(...))
  - SendHeartbeat   → WorkerPool.update(...) with live utilisation; returns the
    server clock (for skew detection) plus any pending directive (DRAIN/SHUTDOWN).
  - DeregisterWorker→ WorkerPool.deregister(...)
  - ExecutionStream → bidi channel: the worker streams TaskUpdates upstream while
    the scheduler streams Tasks downstream from a per-worker outbound queue.
    Tasks are enqueued via ``dispatch(worker_id, task)``.

Directives let the scheduler drain/stop a worker through the heartbeat reply —
set via ``set_directive(worker_id, directive)``.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2 as task_pb
from aeos.core.v1 import worker_pb2 as pb
from aeos.core.v1 import worker_pb2_grpc as pb_grpc

from app.distributed.pool.metrics import WorkerSnapshot
from app.distributed.pool.worker_pool import WorkerPool

from ._util import duration_from_seconds, now_ts

logger = logging.getLogger(__name__)


class WorkerServiceServicer(pb_grpc.WorkerServiceServicer):
    def __init__(
        self,
        pool: WorkerPool | None = None,
        *,
        heartbeat_interval_seconds: float = 5.0,
        on_update=None,
    ) -> None:
        self._pool = pool or WorkerPool()
        self._hb_interval = heartbeat_interval_seconds
        self._on_update = on_update  # optional coroutine(TaskUpdate) sink
        self._directives: dict[str, int] = {}
        self._outbound: dict[str, asyncio.Queue] = {}

    @property
    def pool(self) -> WorkerPool:
        return self._pool

    async def RegisterWorker(self, request, context):  # noqa: N802
        caps = request.capabilities
        host, _, port = (request.node_address or "").partition(":")
        snap = WorkerSnapshot(
            node_id=request.worker_id,
            host=host or "127.0.0.1",
            port=int(port) if port.isdigit() else 9000,
            capabilities=frozenset(caps.supported_task_types),
            max_in_flight=caps.max_concurrency or 16,
        )
        await self._pool.register(snap)
        logger.info("WorkerService: registered %s (%d task types)",
                    request.worker_id, len(caps.supported_task_types))
        resp = pb.RegisterWorkerResponse(
            cluster_member_id=request.worker_id,
            dispatch_concurrency_limit=caps.max_concurrency or 16,
        )
        resp.heartbeat_interval.CopyFrom(duration_from_seconds(self._hb_interval))
        return resp

    async def SendHeartbeat(self, request, context):  # noqa: N802
        snap = await self._pool.get(request.worker_id)
        if snap is None:
            snap = WorkerSnapshot(node_id=request.worker_id)
        snap.in_flight_tasks = request.active_task_count
        snap.cpu_utilization = request.cpu_utilization
        snap.memory_utilization = request.memory_utilization
        snap.is_healthy = request.status != pb.WORKER_STATUS_OFFLINE
        await self._pool.update(snap)
        resp = pb.HeartbeatResponse(
            directive=self._directives.get(request.worker_id, pb.WORKER_DIRECTIVE_NONE),
        )
        resp.server_time.CopyFrom(now_ts())
        return resp

    async def DeregisterWorker(self, request, context):  # noqa: N802
        await self._pool.deregister(request.worker_id)
        self._directives.pop(request.worker_id, None)
        logger.info("WorkerService: deregistered %s (%s)",
                    request.worker_id, request.reason)
        return pb.DeregisterWorkerResponse(acknowledged=True)

    async def ExecutionStream(self, request_iterator, context):  # noqa: N802
        """Bidi: consume worker TaskUpdates while streaming Tasks downstream.

        The first update the worker sends is expected to carry its identity via
        progress.worker_id / result.worker_id; we bind the outbound queue to it.
        """
        worker_id: str | None = None
        outbound: asyncio.Queue | None = None

        async def _pump_updates() -> None:
            nonlocal worker_id, outbound
            async for update in request_iterator:
                wid = ""
                if update.HasField("progress"):
                    wid = update.progress.worker_id
                elif update.HasField("result"):
                    wid = update.result.worker_id
                if wid and worker_id is None:
                    worker_id = wid
                    outbound = self._ensure_outbound(wid)
                if self._on_update is not None:
                    await self._on_update(update)

        pump = asyncio.create_task(_pump_updates())
        try:
            # Wait until we know which worker this stream belongs to.
            for _ in range(200):
                if outbound is not None:
                    break
                await asyncio.sleep(0.01)
            if outbound is None:
                return
            while True:
                task: task_pb.Task = await outbound.get()
                yield task
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump

    # ── scheduler-side controls ───────────────────────────────────────────────
    def dispatch(self, worker_id: str, task: "task_pb.Task") -> None:
        """Enqueue a Task for delivery on the worker's ExecutionStream."""
        self._ensure_outbound(worker_id).put_nowait(task)

    def set_directive(self, worker_id: str, directive: int) -> None:
        self._directives[worker_id] = directive

    def _ensure_outbound(self, worker_id: str) -> asyncio.Queue:
        q = self._outbound.get(worker_id)
        if q is None:
            q = asyncio.Queue()
            self._outbound[worker_id] = q
        return q
