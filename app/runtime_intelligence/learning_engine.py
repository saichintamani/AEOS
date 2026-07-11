"""
Wave 9B.3.6 — Runtime Learning Engine

Records execution outcomes and uses them to:
  - Update per-worker per-task-type success rate estimates
  - Track model performance per task type
  - Detect performance regressions

DefaultLearningEngine — in-memory implementation of LearningEngine ABC
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import ExecutionRecord, LearningEngine

logger = logging.getLogger(__name__)

# Exponential moving average smoothing factor
_EMA_ALPHA = 0.15


@dataclass
class WorkerTaskStats:
    worker_id: str
    task_type: str
    total: int = 0
    successes: int = 0
    ema_success_rate: float = 1.0
    ema_latency_ms: float = 0.0
    ema_cost: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total > 0 else 1.0


@dataclass
class ModelStats:
    model: str
    task_type: str
    total: int = 0
    successes: int = 0
    ema_latency_ms: float = 0.0
    ema_cost: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successes / self.total if self.total > 0 else 1.0


class DefaultLearningEngine(LearningEngine):
    """
    In-memory runtime learning engine.

    Aggregates execution records into:
      - Worker × task_type success rates (EMA)
      - Model × task_type performance (EMA)
    """

    def __init__(self) -> None:
        # (worker_id, task_type) → WorkerTaskStats
        self._worker_stats: dict[tuple[str, str], WorkerTaskStats] = {}
        # (model, task_type) → ModelStats
        self._model_stats: dict[tuple[str, str], ModelStats] = {}
        self._records: list[ExecutionRecord] = []
        self._lock = asyncio.Lock()

    async def record(self, record: ExecutionRecord) -> None:
        async with self._lock:
            self._records.append(record)
            self._update_worker_stats(record)
            if record.model_used:
                self._update_model_stats(record)

    async def predict_success_rate(self, worker_id: str, task_type: str) -> float:
        async with self._lock:
            stats = self._worker_stats.get((worker_id, task_type))
            if stats is None or stats.total == 0:
                return 1.0   # optimistic prior for unknown workers
            return round(stats.ema_success_rate, 4)

    async def recommend_model(self, task_type: str) -> str | None:
        async with self._lock:
            candidates = [
                s for (_, tt), s in self._model_stats.items()
                if tt == task_type and s.total >= 3
            ]
            if not candidates:
                return None
            # Rank by EMA success_rate, break ties by lower EMA latency
            best = max(
                candidates,
                key=lambda s: (s.success_rate, -s.ema_latency_ms),
            )
            return best.model

    async def worker_stats(self, worker_id: str) -> list[WorkerTaskStats]:
        async with self._lock:
            return [s for (wid, _), s in self._worker_stats.items() if wid == worker_id]

    async def all_worker_stats(self) -> dict[tuple[str, str], WorkerTaskStats]:
        async with self._lock:
            return dict(self._worker_stats)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _update_worker_stats(self, rec: ExecutionRecord) -> None:
        key = (rec.worker_id, rec.task_type)
        if key not in self._worker_stats:
            self._worker_stats[key] = WorkerTaskStats(
                worker_id=rec.worker_id,
                task_type=rec.task_type,
                ema_success_rate=1.0 if rec.success else 0.0,
                ema_latency_ms=rec.latency_ms,
                ema_cost=rec.cost,
                total=1,
                successes=1 if rec.success else 0,
            )
            return

        s = self._worker_stats[key]
        s.total += 1
        if rec.success:
            s.successes += 1
        outcome = 1.0 if rec.success else 0.0
        s.ema_success_rate = _ema(s.ema_success_rate, outcome)
        if rec.latency_ms > 0:
            s.ema_latency_ms = _ema(s.ema_latency_ms, rec.latency_ms)
        if rec.cost > 0:
            s.ema_cost = _ema(s.ema_cost, rec.cost)

    def _update_model_stats(self, rec: ExecutionRecord) -> None:
        key = (rec.model_used, rec.task_type)
        if key not in self._model_stats:
            self._model_stats[key] = ModelStats(
                model=rec.model_used,
                task_type=rec.task_type,
                total=1,
                successes=1 if rec.success else 0,
                ema_latency_ms=rec.latency_ms,
                ema_cost=rec.cost,
            )
            return

        s = self._model_stats[key]
        s.total += 1
        if rec.success:
            s.successes += 1
        if rec.latency_ms > 0:
            s.ema_latency_ms = _ema(s.ema_latency_ms, rec.latency_ms)
        if rec.cost > 0:
            s.ema_cost = _ema(s.ema_cost, rec.cost)


def _ema(current: float, new_value: float, alpha: float = _EMA_ALPHA) -> float:
    return alpha * new_value + (1 - alpha) * current
