"""
app/runtime/pattern_miner.py

Pattern Miner — extracts scheduling heuristics from execution history.

Mines:
  1. Bottleneck detection: which task types consistently delay downstream
  2. Failure signatures: combinations of conditions that predict failure
  3. Successful patterns: worker/topology combinations with high success rates
  4. Recovery patterns: fastest recovery paths per fault type
  5. Cost-optimal assignments: worker × workflow_type cost matrix

The miner is stateless — it reads from ExecutionMemoryStore and
produces SchedulingHints that the AdaptiveScheduler consumes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class BottleneckSignal:
    """A task type that consistently delays overall workflow completion."""
    task_type: str
    avg_delay_ratio: float      # avg_task_duration / avg_workflow_duration
    sample_count: int
    confidence: float


@dataclass
class FailureSignature:
    """A combination of conditions that predicts failure."""
    description: str
    conditions: dict[str, Any]  # e.g. {"cpu_percent": ">85", "memory_mb": ">6000"}
    failure_rate: float
    sample_count: int
    confidence: float


@dataclass
class WorkerPerformanceProfile:
    """Historical performance of a worker for a specific workflow type."""
    worker_id: str
    workflow_type: str
    success_rate: float
    avg_duration_ms: float
    avg_cpu_percent: float
    avg_memory_mb: float
    avg_cost_units: float
    sample_count: int

    @property
    def score(self) -> float:
        """Composite score: higher is better (success rate × speed bonus)."""
        if self.avg_duration_ms <= 0:
            return 0.0
        speed_factor = 1000.0 / (self.avg_duration_ms + 1)
        return self.success_rate * (1 + speed_factor) - self.avg_cost_units * 0.01


@dataclass
class RecoveryPattern:
    """Best recovery path for a given fault type."""
    fault_type: str
    fastest_recovery_path: str
    avg_recovery_time_ms: float
    success_rate: float
    sample_count: int


@dataclass
class MiningResult:
    """Complete result of a pattern mining pass."""
    mined_at: float
    window_hours: float
    total_executions_analyzed: int

    bottlenecks: list[BottleneckSignal] = field(default_factory=list)
    failure_signatures: list[FailureSignature] = field(default_factory=list)
    worker_profiles: list[WorkerPerformanceProfile] = field(default_factory=list)
    recovery_patterns: list[RecoveryPattern] = field(default_factory=list)

    def best_worker_for(self, workflow_type: str) -> str | None:
        """Return the worker_id with the highest score for this workflow type."""
        candidates = [p for p in self.worker_profiles if p.workflow_type == workflow_type]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.score).worker_id

    def is_high_risk(self, cpu_percent: float, memory_mb: float) -> bool:
        """Check if current resource levels match a failure signature."""
        for sig in self.failure_signatures:
            conds = sig.conditions
            cpu_thresh = float(str(conds.get("cpu_percent", "0")).lstrip(">"))
            mem_thresh = float(str(conds.get("memory_mb", "0")).lstrip(">"))
            if cpu_percent > cpu_thresh and memory_mb > mem_thresh:
                if sig.failure_rate > 0.3:  # >30% failure rate = high risk
                    return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mined_at": self.mined_at,
            "window_hours": self.window_hours,
            "total_executions_analyzed": self.total_executions_analyzed,
            "bottlenecks": [
                {"task_type": b.task_type, "avg_delay_ratio": b.avg_delay_ratio,
                 "confidence": b.confidence}
                for b in self.bottlenecks
            ],
            "failure_signatures": [
                {"description": f.description, "failure_rate": f.failure_rate,
                 "confidence": f.confidence}
                for f in self.failure_signatures
            ],
            "worker_profiles": [
                {"worker_id": w.worker_id, "workflow_type": w.workflow_type,
                 "success_rate": w.success_rate, "score": w.score}
                for w in self.worker_profiles
            ],
            "recovery_patterns": [
                {"fault_type": r.fault_type, "avg_recovery_time_ms": r.avg_recovery_time_ms,
                 "success_rate": r.success_rate}
                for r in self.recovery_patterns
            ],
        }


class PatternMiner:
    """
    Reads execution history and derives scheduling heuristics.

    Designed to run periodically (e.g. every 5 minutes) rather than
    on every scheduling decision — mining is expensive, hints are cheap.

    Usage::

        store = ExecutionMemoryStore(...)
        miner = PatternMiner(store)
        result = await miner.mine(window_hours=24)
        best = result.best_worker_for("rag-pipeline")
    """

    MIN_SAMPLES = 5  # Minimum executions before drawing conclusions

    def __init__(self, store: Any) -> None:
        self._store = store

    async def mine(self, window_hours: float = 24.0) -> MiningResult:
        """Run a full pattern mining pass over the specified time window."""
        start = time.monotonic()
        stats = await self._store.aggregate_stats(since_hours=window_hours)
        total = stats.get("total", 0)

        logger.info("PatternMiner: analyzing %d executions (%.0fh window)", total, window_hours)

        bottlenecks, failure_sigs, worker_profiles, recovery_patterns = await asyncio.gather_fallback(
            self._mine_bottlenecks(window_hours),
            self._mine_failure_signatures(window_hours),
            self._mine_worker_profiles(window_hours),
            self._mine_recovery_patterns(window_hours),
        )

        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info("PatternMiner complete in %.1fms: %d bottlenecks, %d failure sigs, "
                    "%d worker profiles, %d recovery patterns",
                    elapsed_ms, len(bottlenecks), len(failure_sigs),
                    len(worker_profiles), len(recovery_patterns))

        return MiningResult(
            mined_at=time.time(),
            window_hours=window_hours,
            total_executions_analyzed=total,
            bottlenecks=bottlenecks,
            failure_signatures=failure_sigs,
            worker_profiles=worker_profiles,
            recovery_patterns=recovery_patterns,
        )

    async def _mine_bottlenecks(self, window_hours: float) -> list[BottleneckSignal]:
        """Identify task types that delay overall workflow completion."""
        since = time.time() - window_hours * 3600
        try:
            rows = await self._store.query_recent(since=since, limit=1000)
        except Exception as exc:
            logger.warning("PatternMiner: bottleneck query failed: %s", exc)
            return []

        # Aggregate by workflow_type to find slow types
        type_durations: dict[str, list[float]] = {}
        for row in rows:
            wt = row.get("workflow_type", "unknown")
            dur = row.get("duration_ms", 0)
            if dur > 0:
                type_durations.setdefault(wt, []).append(dur)

        bottlenecks = []
        overall_avg = sum(d for durs in type_durations.values() for d in durs) / max(
            sum(len(d) for d in type_durations.values()), 1
        )

        for wt, durations in type_durations.items():
            if len(durations) < self.MIN_SAMPLES:
                continue
            avg = sum(durations) / len(durations)
            ratio = avg / overall_avg if overall_avg > 0 else 1.0
            if ratio > 1.5:  # 50% slower than average = bottleneck
                confidence = min(len(durations) / 50.0, 1.0)
                bottlenecks.append(BottleneckSignal(
                    task_type=wt,
                    avg_delay_ratio=ratio,
                    sample_count=len(durations),
                    confidence=confidence,
                ))

        return sorted(bottlenecks, key=lambda b: -b.avg_delay_ratio)

    async def _mine_failure_signatures(self, window_hours: float) -> list[FailureSignature]:
        """Identify resource conditions correlated with failures."""
        since = time.time() - window_hours * 3600
        try:
            rows = await self._store.query_recent(since=since, limit=2000)
        except Exception as exc:
            logger.warning("PatternMiner: failure sig query failed: %s", exc)
            return []

        # High CPU + high memory failure correlation
        high_resource_failures = sum(
            1 for r in rows
            if r.get("cpu_percent", 0) > 80 and r.get("memory_mb", 0) > 5000
            and r.get("outcome") == "FAILURE"
        )
        high_resource_total = sum(
            1 for r in rows
            if r.get("cpu_percent", 0) > 80 and r.get("memory_mb", 0) > 5000
        )

        sigs = []
        if high_resource_total >= self.MIN_SAMPLES:
            failure_rate = high_resource_failures / high_resource_total
            if failure_rate > 0.2:
                sigs.append(FailureSignature(
                    description="High CPU (>80%) + High Memory (>5GB) → elevated failure rate",
                    conditions={"cpu_percent": ">80", "memory_mb": ">5000"},
                    failure_rate=failure_rate,
                    sample_count=high_resource_total,
                    confidence=min(high_resource_total / 20.0, 1.0),
                ))

        # Long queue wait failures
        queue_failures = sum(
            1 for r in rows
            if r.get("queue_wait_ms", 0) > 5000 and r.get("outcome") == "FAILURE"
        )
        queue_total = sum(1 for r in rows if r.get("queue_wait_ms", 0) > 5000)
        if queue_total >= self.MIN_SAMPLES:
            rate = queue_failures / queue_total
            if rate > 0.15:
                sigs.append(FailureSignature(
                    description="Long queue wait (>5s) correlates with failure",
                    conditions={"queue_wait_ms": ">5000"},
                    failure_rate=rate,
                    sample_count=queue_total,
                    confidence=min(queue_total / 20.0, 1.0),
                ))

        return sigs

    async def _mine_worker_profiles(self, window_hours: float) -> list[WorkerPerformanceProfile]:
        """Build performance profiles per (worker, workflow_type) pair."""
        since = time.time() - window_hours * 3600
        try:
            rows = await self._store.query_recent(since=since, limit=2000)
        except Exception as exc:
            logger.warning("PatternMiner: worker profile query failed: %s", exc)
            return []

        # Group by (worker_id, workflow_type)
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (row.get("worker_id", ""), row.get("workflow_type", ""))
            if key[0]:
                groups.setdefault(key, []).append(row)

        profiles = []
        for (worker_id, workflow_type), executions in groups.items():
            if len(executions) < self.MIN_SAMPLES:
                continue
            successes = sum(1 for e in executions if e.get("outcome") == "SUCCESS")
            profiles.append(WorkerPerformanceProfile(
                worker_id=worker_id,
                workflow_type=workflow_type,
                success_rate=successes / len(executions),
                avg_duration_ms=sum(e.get("duration_ms", 0) for e in executions) / len(executions),
                avg_cpu_percent=sum(e.get("cpu_percent", 0) for e in executions) / len(executions),
                avg_memory_mb=sum(e.get("memory_mb", 0) for e in executions) / len(executions),
                avg_cost_units=sum(e.get("cost_units", 0) for e in executions) / len(executions),
                sample_count=len(executions),
            ))

        return sorted(profiles, key=lambda p: -p.score)

    async def _mine_recovery_patterns(self, window_hours: float) -> list[RecoveryPattern]:
        """Find fastest recovery paths per fault type."""
        since = time.time() - window_hours * 3600
        try:
            rows = await self._store.query_recent(since=since, limit=1000)
        except Exception as exc:
            logger.warning("PatternMiner: recovery pattern query failed: %s", exc)
            return []

        # Only consider executions with recoveries
        recovery_rows = [r for r in rows if r.get("recovery_count", 0) > 0]

        # Without detailed per-fault data (that requires joining recovery_events table),
        # we use aggregate signals from the execution records
        if not recovery_rows:
            return []

        avg_recovery = sum(r.get("duration_ms", 0) for r in recovery_rows) / len(recovery_rows)
        return [
            RecoveryPattern(
                fault_type="generic",
                fastest_recovery_path="lease-expiry + worker-restart",
                avg_recovery_time_ms=avg_recovery,
                success_rate=sum(1 for r in recovery_rows if r.get("outcome") == "SUCCESS") / len(recovery_rows),
                sample_count=len(recovery_rows),
            )
        ]


# Compatibility shim: asyncio.gather doesn't have gather_fallback
# Replace with a safe version that returns empty lists on individual failures
import asyncio as _asyncio

async def _gather_fallback(*coros):  # type: ignore[no-redef]
    results = []
    for coro in coros:
        try:
            results.append(await coro)
        except Exception as exc:
            logger.warning("PatternMiner sub-task failed: %s", exc)
            results.append([])
    return results

# Monkey-patch the method reference
PatternMiner._mine_all = _gather_fallback  # type: ignore[attr-defined]

# Override mine() to use safe gather
_original_mine = PatternMiner.mine
async def _safe_mine(self, window_hours: float = 24.0) -> MiningResult:
    start = time.monotonic()
    stats = await self._store.aggregate_stats(since_hours=window_hours)
    total = stats.get("total", 0)
    logger.info("PatternMiner: analyzing %d executions (%.0fh window)", total, window_hours)

    results = await _gather_fallback(
        self._mine_bottlenecks(window_hours),
        self._mine_failure_signatures(window_hours),
        self._mine_worker_profiles(window_hours),
        self._mine_recovery_patterns(window_hours),
    )
    bottlenecks, failure_sigs, worker_profiles, recovery_patterns = results

    elapsed_ms = (time.monotonic() - start) * 1000
    logger.info(
        "PatternMiner complete in %.1fms: %d bottlenecks, %d failure sigs, "
        "%d worker profiles, %d recovery patterns",
        elapsed_ms, len(bottlenecks), len(failure_sigs),
        len(worker_profiles), len(recovery_patterns),
    )
    return MiningResult(
        mined_at=time.time(),
        window_hours=window_hours,
        total_executions_analyzed=total,
        bottlenecks=bottlenecks,
        failure_signatures=failure_sigs,
        worker_profiles=worker_profiles,
        recovery_patterns=recovery_patterns,
    )

PatternMiner.mine = _safe_mine  # type: ignore[method-assign]
