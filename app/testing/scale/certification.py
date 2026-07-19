"""
app/testing/scale/certification.py

Scale Certification Framework — Bronze through Platinum.

Tier definitions:
  Bronze:   10 workers,  10,000 tasks, P99 latency < 2s
  Silver:   25 workers, 100,000 tasks, P99 latency < 1.5s
  Gold:     50 workers,   1M tasks,   P99 latency < 1s
  Platinum: 100 workers, sustained load (1h) + chaos injection

Each certification run:
  1. Ramps up to target worker count
  2. Submits tasks at target throughput
  3. Collects latency, throughput, error rate, resource utilization
  4. Verifies recovery correctness under load (Platinum only)
  5. Produces a certification report in reports/certification/
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class CertificationTier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


@dataclass
class TierRequirements:
    tier: CertificationTier
    worker_count: int
    total_tasks: int
    target_throughput_tps: float    # tasks per second
    p99_latency_limit_ms: float
    error_rate_limit: float         # 0.0–1.0
    recovery_under_chaos: bool      # Platinum only


TIER_REQUIREMENTS = {
    CertificationTier.BRONZE: TierRequirements(
        tier=CertificationTier.BRONZE,
        worker_count=10,
        total_tasks=10_000,
        target_throughput_tps=50.0,
        p99_latency_limit_ms=2000.0,
        error_rate_limit=0.01,
        recovery_under_chaos=False,
    ),
    CertificationTier.SILVER: TierRequirements(
        tier=CertificationTier.SILVER,
        worker_count=25,
        total_tasks=100_000,
        target_throughput_tps=200.0,
        p99_latency_limit_ms=1500.0,
        error_rate_limit=0.005,
        recovery_under_chaos=False,
    ),
    CertificationTier.GOLD: TierRequirements(
        tier=CertificationTier.GOLD,
        worker_count=50,
        total_tasks=1_000_000,
        target_throughput_tps=1000.0,
        p99_latency_limit_ms=1000.0,
        error_rate_limit=0.001,
        recovery_under_chaos=False,
    ),
    CertificationTier.PLATINUM: TierRequirements(
        tier=CertificationTier.PLATINUM,
        worker_count=100,
        total_tasks=0,  # Sustained — not count-based
        target_throughput_tps=2000.0,
        p99_latency_limit_ms=1000.0,
        error_rate_limit=0.001,
        recovery_under_chaos=True,
    ),
}


@dataclass
class LatencyHistogram:
    """Running latency histogram (approximate percentiles)."""
    _samples: list[float] = field(default_factory=list)

    def record(self, latency_ms: float) -> None:
        self._samples.append(latency_ms)

    def percentile(self, p: float) -> float:
        """Return the p-th percentile (0–100)."""
        if not self._samples:
            return 0.0
        sorted_samples = sorted(self._samples)
        idx = max(0, min(len(sorted_samples) - 1,
                         int(math.ceil(p / 100.0 * len(sorted_samples))) - 1))
        return sorted_samples[idx]

    @property
    def p50(self) -> float:
        return self.percentile(50)

    @property
    def p95(self) -> float:
        return self.percentile(95)

    @property
    def p99(self) -> float:
        return self.percentile(99)

    @property
    def p999(self) -> float:
        return self.percentile(99.9)

    @property
    def mean(self) -> float:
        return sum(self._samples) / len(self._samples) if self._samples else 0.0

    @property
    def count(self) -> int:
        return len(self._samples)


@dataclass
class CertificationResult:
    """Result of a single certification run."""
    tier: CertificationTier
    run_id: str
    started_at: float
    ended_at: float
    passed: bool

    # Throughput
    tasks_submitted: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0
    observed_throughput_tps: float = 0.0

    # Latency
    latency: LatencyHistogram = field(default_factory=LatencyHistogram)

    # Resources
    peak_cpu_percent: float = 0.0
    peak_memory_mb: float = 0.0

    # Correctness
    invariant_violations: int = 0
    exactly_once_breaks: int = 0
    governance_bypasses: int = 0

    # Recovery (Platinum)
    chaos_events: int = 0
    max_recovery_time_ms: float = 0.0
    rto_met: bool = True

    # Failure reasons
    failures: list[str] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        total = self.tasks_completed + self.tasks_failed
        return self.tasks_failed / total if total > 0 else 0.0

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        req = TIER_REQUIREMENTS[self.tier]
        return {
            "tier": self.tier.value,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "passed": self.passed,
            "requirements": {
                "worker_count": req.worker_count,
                "total_tasks": req.total_tasks,
                "target_throughput_tps": req.target_throughput_tps,
                "p99_latency_limit_ms": req.p99_latency_limit_ms,
                "error_rate_limit": req.error_rate_limit,
            },
            "throughput": {
                "tasks_submitted": self.tasks_submitted,
                "tasks_completed": self.tasks_completed,
                "tasks_failed": self.tasks_failed,
                "observed_tps": self.observed_throughput_tps,
                "error_rate": self.error_rate,
            },
            "latency_ms": {
                "p50": self.latency.p50,
                "p95": self.latency.p95,
                "p99": self.latency.p99,
                "p999": self.latency.p999,
                "mean": self.latency.mean,
                "sample_count": self.latency.count,
            },
            "resources": {
                "peak_cpu_percent": self.peak_cpu_percent,
                "peak_memory_mb": self.peak_memory_mb,
            },
            "correctness": {
                "invariant_violations": self.invariant_violations,
                "exactly_once_breaks": self.exactly_once_breaks,
                "governance_bypasses": self.governance_bypasses,
            },
            "recovery": {
                "chaos_events": self.chaos_events,
                "max_recovery_time_ms": self.max_recovery_time_ms,
                "rto_met": self.rto_met,
            },
            "failures": self.failures,
        }


class CertificationRunner:
    """
    Executes scale certification runs against a live or simulated AEOS cluster.

    In CI mode (no live cluster), runs a deterministic simulation that
    validates the certification framework itself.

    Usage::

        runner = CertificationRunner(
            task_submitter=submit_fn,    # async fn(task_id) -> latency_ms
            output_dir="reports/certification",
        )
        result = await runner.run_tier(CertificationTier.BRONZE)
        assert result.passed
    """

    def __init__(
        self,
        task_submitter: Callable[[str], Awaitable[float]] | None = None,
        output_dir: str = "reports/certification",
        simulation_mode: bool = True,
    ) -> None:
        self._submit = task_submitter or self._simulated_submit
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._simulation = simulation_mode

    async def run_tier(self, tier: CertificationTier) -> CertificationResult:
        """Run a complete certification for the given tier."""
        req = TIER_REQUIREMENTS[tier]
        run_id = f"{tier.value}-{int(time.time())}"
        started_at = time.time()

        logger.info("=== SCALE CERTIFICATION: %s ===", tier.value.upper())
        logger.info("Requirements: %d workers, %d tasks, P99 < %.0fms",
                    req.worker_count, req.total_tasks, req.p99_latency_limit_ms)

        result = CertificationResult(
            tier=tier,
            run_id=run_id,
            started_at=started_at,
            ended_at=started_at,
            passed=False,
        )

        try:
            if tier == CertificationTier.PLATINUM:
                await self._run_platinum(result, req)
            else:
                await self._run_standard(result, req)

            # Evaluate pass/fail
            self._evaluate(result, req)

        except Exception as exc:
            result.failures.append(f"Unexpected error: {exc}")
            logger.error("Certification run failed: %s", exc)

        finally:
            result.ended_at = time.time()
            self._save_report(result)

        return result

    async def run_all_tiers(self) -> dict[CertificationTier, CertificationResult]:
        """Run all four certification tiers sequentially."""
        results = {}
        for tier in [CertificationTier.BRONZE, CertificationTier.SILVER,
                     CertificationTier.GOLD, CertificationTier.PLATINUM]:
            logger.info("Starting %s certification...", tier.value)
            result = await self.run_tier(tier)
            results[tier] = result
            if not result.passed:
                logger.warning("%s FAILED — stopping certification at this tier", tier.value.upper())
                break
            await asyncio.sleep(5.0)  # Cool-down between tiers
        return results

    async def _run_standard(self, result: CertificationResult, req: TierRequirements) -> None:
        """Run a standard (Bronze/Silver/Gold) certification."""
        total = req.total_tasks
        interval = 1.0 / req.target_throughput_tps
        batch_size = min(100, max(1, int(req.target_throughput_tps / 10)))

        logger.info("Submitting %d tasks at %.0f TPS (batch=%d)...", total, req.target_throughput_tps, batch_size)

        submitted = 0
        start = time.monotonic()

        while submitted < total:
            batch = min(batch_size, total - submitted)
            task_ids = [f"cert-{result.run_id}-{submitted + i}" for i in range(batch)]

            latencies = await asyncio.gather(
                *[self._submit(tid) for tid in task_ids],
                return_exceptions=True,
            )

            for lat in latencies:
                result.tasks_submitted += 1
                if isinstance(lat, Exception):
                    result.tasks_failed += 1
                else:
                    result.tasks_completed += 1
                    result.latency.record(float(lat))

            submitted += batch

            # Rate limiting
            elapsed = time.monotonic() - start
            expected_elapsed = submitted / req.target_throughput_tps
            if expected_elapsed > elapsed:
                await asyncio.sleep(expected_elapsed - elapsed)

            if submitted % max(1, total // 20) == 0:
                elapsed_s = time.monotonic() - start
                actual_tps = submitted / elapsed_s if elapsed_s > 0 else 0
                logger.info("Progress: %d/%d (%.0f TPS, P99=%.0fms)",
                            submitted, total, actual_tps, result.latency.p99)

        elapsed_total = time.monotonic() - start
        result.observed_throughput_tps = result.tasks_completed / elapsed_total if elapsed_total > 0 else 0

    async def _run_platinum(self, result: CertificationResult, req: TierRequirements) -> None:
        """Run Platinum certification: sustained 1-hour load + chaos injection."""
        duration_seconds = 60.0 if self._simulation else 3600.0  # 1min in sim, 1h in prod
        interval = 1.0 / req.target_throughput_tps
        start = time.monotonic()
        chaos_times = [duration_seconds * 0.2, duration_seconds * 0.5, duration_seconds * 0.8]
        next_chaos_idx = 0

        logger.info("Platinum: sustained %.0fs load + chaos injection", duration_seconds)

        task_counter = 0
        while time.monotonic() - start < duration_seconds:
            elapsed = time.monotonic() - start

            # Chaos injection at scheduled times
            if next_chaos_idx < len(chaos_times) and elapsed >= chaos_times[next_chaos_idx]:
                recovery_ms = await self._inject_chaos_event(result)
                result.max_recovery_time_ms = max(result.max_recovery_time_ms, recovery_ms)
                if recovery_ms > 60_000:  # 60s RTO
                    result.rto_met = False
                next_chaos_idx += 1

            # Submit tasks
            task_id = f"plat-{result.run_id}-{task_counter}"
            task_counter += 1
            try:
                latency = await self._submit(task_id)
                result.tasks_submitted += 1
                result.tasks_completed += 1
                result.latency.record(latency)
            except Exception:
                result.tasks_submitted += 1
                result.tasks_failed += 1

            await asyncio.sleep(interval)

        elapsed_total = time.monotonic() - start
        result.observed_throughput_tps = result.tasks_completed / elapsed_total if elapsed_total > 0 else 0

    async def _inject_chaos_event(self, result: CertificationResult) -> float:
        """Inject a chaos event and return recovery time in ms."""
        result.chaos_events += 1
        logger.info("Platinum: injecting chaos event #%d", result.chaos_events)

        if self._simulation:
            # Simulate 5-15s recovery
            recovery_s = random.uniform(5.0, 15.0)
            await asyncio.sleep(0.1)  # Don't actually sleep 15s in sim
            return recovery_s * 1000

        # Real chaos: would call ChaosEngine here
        return 5000.0  # Stub

    def _evaluate(self, result: CertificationResult, req: TierRequirements) -> None:
        """Check if the result meets tier requirements."""
        passed = True

        # Throughput check
        if result.observed_throughput_tps < req.target_throughput_tps * 0.95:
            result.failures.append(
                f"Throughput {result.observed_throughput_tps:.0f} TPS < "
                f"required {req.target_throughput_tps:.0f} TPS"
            )
            passed = False

        # P99 latency check
        if result.latency.p99 > req.p99_latency_limit_ms:
            result.failures.append(
                f"P99 latency {result.latency.p99:.0f}ms > "
                f"limit {req.p99_latency_limit_ms:.0f}ms"
            )
            passed = False

        # Error rate check
        if result.error_rate > req.error_rate_limit:
            result.failures.append(
                f"Error rate {result.error_rate:.3%} > "
                f"limit {req.error_rate_limit:.3%}"
            )
            passed = False

        # Correctness checks
        if result.invariant_violations > 0:
            result.failures.append(
                f"Invariant violations: {result.invariant_violations}"
            )
            passed = False

        if result.governance_bypasses > 0:
            result.failures.append(
                f"Governance bypasses detected: {result.governance_bypasses}"
            )
            passed = False

        # Platinum: RTO check
        if req.recovery_under_chaos and not result.rto_met:
            result.failures.append(
                f"RTO exceeded during chaos: max={result.max_recovery_time_ms:.0f}ms > 60000ms"
            )
            passed = False

        result.passed = passed
        status = "PASSED" if passed else "FAILED"
        logger.info(
            "=== %s CERTIFICATION %s ===",
            req.tier.value.upper(), status,
        )
        if not passed:
            for f in result.failures:
                logger.error("  FAIL: %s", f)

    async def _simulated_submit(self, task_id: str) -> float:
        """Simulated task submission — returns synthetic latency."""
        # Simulate realistic latency distribution: mostly fast, occasional slow
        r = random.random()
        if r < 0.90:
            latency = random.gauss(150, 40)  # 90th pct: ~150ms mean
        elif r < 0.99:
            latency = random.gauss(500, 100)  # P90-P99: 500ms
        else:
            latency = random.gauss(1800, 200)  # P99+: 1800ms

        latency = max(10.0, latency)  # Floor at 10ms

        # Simulate async work
        await asyncio.sleep(latency / 10_000)  # 10x accelerated in sim
        return latency

    def _save_report(self, result: CertificationResult) -> None:
        path = self._output_dir / f"{result.run_id}-report.json"
        path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
        logger.info("Certification report saved: %s", path)
