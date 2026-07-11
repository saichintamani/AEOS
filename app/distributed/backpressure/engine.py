"""
Backpressure engine.

Samples the WorkerPool every eval_interval_seconds, evaluates the policy,
and exposes should_reject(), delay_seconds(), wait_if_throttled() to callers.

BackpressureState mirrors BackpressureAction but with NORMAL for the idle case.

Contract: AC-SCHED-001
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum

from app.distributed.backpressure.policy import BackpressureAction, ThresholdPolicy
from app.distributed.pool.worker_pool import WorkerPool

logger = logging.getLogger(__name__)


class BackpressureState(str, Enum):
    NORMAL    = "normal"
    SLOWING   = "slowing"
    REJECTING = "rejecting"
    SCALING   = "scaling"
    ALERTING  = "alerting"


_ACTION_TO_STATE: dict[BackpressureAction, BackpressureState] = {
    BackpressureAction.NONE:   BackpressureState.NORMAL,
    BackpressureAction.SLOW:   BackpressureState.SLOWING,
    BackpressureAction.REJECT: BackpressureState.REJECTING,
    BackpressureAction.SCALE:  BackpressureState.SCALING,
    BackpressureAction.ALERT:  BackpressureState.ALERTING,
}


class BackpressureEngine:
    """
    Periodically evaluates backpressure and exposes control points.

    Usage:
        engine = BackpressureEngine(pool, ThresholdPolicy())
        await engine.start()
        ...
        if engine.should_reject():
            raise BackpressureError(...)
        await engine.wait_if_throttled()
    """

    def __init__(
        self,
        pool: WorkerPool,
        policy: ThresholdPolicy,
        *,
        eval_interval_seconds: float = 2.0,
    ) -> None:
        self._pool = pool
        self._policy = policy
        self._interval = eval_interval_seconds
        self._state: BackpressureState = BackpressureState.NORMAL
        self._task: asyncio.Task | None = None
        self._running = False

    @property
    def state(self) -> BackpressureState:
        return self._state

    def should_reject(self) -> bool:
        return self._state == BackpressureState.REJECTING

    def delay_seconds(self) -> float:
        return self._policy.slow_delay if self._state == BackpressureState.SLOWING else 0.0

    async def wait_if_throttled(self) -> None:
        delay = self.delay_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

    async def evaluate_once(self) -> BackpressureState:
        snapshots = await self._pool.all_snapshots()
        action = self._policy.evaluate(snapshots)
        self._state = _ACTION_TO_STATE.get(action, BackpressureState.NORMAL)
        return self._state

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="backpressure-engine")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.evaluate_once()
            except Exception:
                logger.exception("Backpressure evaluation error")
            await asyncio.sleep(self._interval)
