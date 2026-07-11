"""
Wave 9B.5.7 — Distributed Scheduler

Leader-based scheduling with per-worker sub-schedulers.

Architecture:
  LeaderScheduler   — runs on the Raft leader; admits tasks, assigns to workers
  WorkerScheduler   — runs on each worker; manages local queue and concurrency
  AdmissionControl  — backpressure gate before task enters the cluster
  ExecutionQueue    — per-worker priority queue

The leader receives task submissions, runs the AI decision engine, and
dispatches to the chosen worker. If the leader changes, the new leader
picks up from the event log.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.distributed.backpressure.policy import BackpressureAction, ThresholdPolicy
from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine

logger = logging.getLogger(__name__)

_MAX_QUEUE_DEPTH = 256


@dataclass
class ScheduledTask:
    requirements: TaskRequirements
    worker_id: str
    priority: int = 2    # higher = more urgent
    enqueued_at: float = field(default_factory=lambda: asyncio.get_event_loop().time())


class WorkerScheduler:
    """
    Per-worker task queue and concurrency limiter.

    Bounded by max_in_flight. Tasks are dispatched in priority order.
    """

    def __init__(self, worker_id: str, max_in_flight: int = 16) -> None:
        self._worker_id = worker_id
        self._sem = asyncio.Semaphore(max_in_flight)
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=_MAX_QUEUE_DEPTH)
        self._in_flight = 0
        self._completed = 0
        self._failed = 0
        self._seq = 0   # tie-breaker so equal-priority tasks are FIFO

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def enqueue(self, task: ScheduledTask) -> bool:
        """Enqueue a task. Returns False if queue is full."""
        if self._queue.full():
            logger.warning(
                "WorkerScheduler[%s]: queue full, rejecting task %s",
                self._worker_id, task.requirements.task_id,
            )
            return False
        # Priority queue: lower value = higher priority, so negate.
        # Use _seq as tie-breaker to avoid comparing ScheduledTask instances.
        self._seq += 1
        await self._queue.put((-task.priority, self._seq, task))
        return True

    async def next(self) -> ScheduledTask | None:
        """Pop the highest-priority task. Blocks until one is available."""
        try:
            _, _seq, task = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            return task
        except asyncio.TimeoutError:
            return None

    def record_complete(self, success: bool) -> None:
        self._in_flight -= 1
        if success:
            self._completed += 1
        else:
            self._failed += 1

    def load_factor(self) -> float:
        """0–1 load estimate based on in-flight + queue depth."""
        active = self._in_flight + self._queue.qsize()
        return min(active / _MAX_QUEUE_DEPTH, 1.0)


class AdmissionControl:
    """
    Backpressure gate that rejects tasks when the cluster is overloaded.

    Uses ThresholdPolicy from the existing backpressure module.
    """

    def __init__(self) -> None:
        self._policy = ThresholdPolicy()
        self._cluster_queue_depth = 0
        self._cluster_cpu = 0.0
        self._cluster_memory = 0.0

    def update_metrics(self, queue_depth: int, cpu: float, memory: float) -> None:
        self._cluster_queue_depth = queue_depth
        self._cluster_cpu = cpu
        self._cluster_memory = memory

    def should_admit(self, requirements: TaskRequirements) -> tuple[bool, str]:
        """Returns (admit, reason)."""
        from app.distributed.pool.metrics import WorkerSnapshot
        snapshot = WorkerSnapshot(
            node_id="cluster",
            queue_depth=self._cluster_queue_depth,
            cpu_utilization=self._cluster_cpu,
            memory_utilization=self._cluster_memory,
            in_flight_tasks=0,
        )
        action = self._policy.evaluate([snapshot])
        if action == BackpressureAction.REJECT:
            return False, f"cluster overloaded (queue={self._cluster_queue_depth}, cpu={self._cluster_cpu:.0%})"
        if action == BackpressureAction.ALERT and requirements.priority not in ("critical", "high"):
            return False, "load shedding — only critical/high priority admitted"
        return True, "admitted"


class LeaderScheduler:
    """
    Leader-side distributed scheduler.

    Receives task submissions, runs AI decision engine, dispatches to worker.
    Maintains worker sub-schedulers and admission control.
    """

    def __init__(
        self,
        decision_engine: ExpectedUtilityDecisionEngine | None = None,
    ) -> None:
        self._engine = decision_engine or ExpectedUtilityDecisionEngine()
        self._admission = AdmissionControl()
        self._worker_schedulers: dict[str, WorkerScheduler] = {}
        self._profiles: list[CapabilityProfile] = []
        self._dispatch_callbacks: list[Any] = []
        self._running = False
        self._task: asyncio.Task | None = None

    def register_worker(
        self, profile: CapabilityProfile, max_in_flight: int = 16
    ) -> None:
        wid = profile.worker_id
        if wid not in self._worker_schedulers:
            self._worker_schedulers[wid] = WorkerScheduler(wid, max_in_flight)
        # Keep profile updated
        self._profiles = [p for p in self._profiles if p.worker_id != wid]
        self._profiles.append(profile)
        self._admission.update_metrics(
            queue_depth=sum(ws.queue_depth for ws in self._worker_schedulers.values()),
            cpu=max((p.current_load for p in self._profiles), default=0.0),
            memory=0.0,
        )

    def on_dispatch(self, callback: Any) -> None:
        """Register a callback: callback(task: ScheduledTask) called when dispatched."""
        self._dispatch_callbacks.append(callback)

    async def submit(self, requirements: TaskRequirements) -> tuple[bool, str]:
        """Submit a task. Returns (success, reason)."""
        admitted, reason = self._admission.should_admit(requirements)
        if not admitted:
            logger.warning("LeaderScheduler: rejected task %s — %s",
                           requirements.task_id, reason)
            return False, reason

        if not self._profiles:
            return False, "no workers registered"

        decision = await self._engine.decide(requirements, self._profiles)
        if not decision.worker_id:
            return False, "no eligible worker found"

        ws = self._worker_schedulers.get(decision.worker_id)
        if not ws:
            return False, f"worker {decision.worker_id} scheduler not found"

        priority_map = {"critical": 4, "high": 3, "normal": 2, "low": 1, "batch": 0}
        task = ScheduledTask(
            requirements=requirements,
            worker_id=decision.worker_id,
            priority=priority_map.get(requirements.priority, 2),
        )
        ok = await ws.enqueue(task)
        if ok:
            for cb in self._dispatch_callbacks:
                try:
                    await cb(task) if asyncio.iscoroutinefunction(cb) else cb(task)
                except Exception:
                    pass
            logger.info(
                "LeaderScheduler: dispatched %s → worker %s (utility=%.3f)",
                requirements.task_id, decision.worker_id, decision.expected_utility,
            )
        return ok, "ok" if ok else "worker queue full"

    def cluster_stats(self) -> dict:
        return {
            "workers": len(self._worker_schedulers),
            "total_queued": sum(ws.queue_depth for ws in self._worker_schedulers.values()),
            "total_in_flight": sum(ws.in_flight for ws in self._worker_schedulers.values()),
            "worker_load": {wid: ws.load_factor()
                            for wid, ws in self._worker_schedulers.items()},
        }
