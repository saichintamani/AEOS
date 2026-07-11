"""
Wave 9B.3.10 — Runtime Digital Twin Simulator

Simulates the full AEOS runtime under configurable load/fault scenarios
before production deployment.

SimulationEngine — runs a SimulationScenario and returns SimulationResult
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    SimulationResult,
    SimulationScenario,
    TaskRequirements,
)
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.learning_engine import DefaultLearningEngine

logger = logging.getLogger(__name__)

_SEED = 42   # reproducible simulations


class SimulationEngine:
    """
    Discrete-event simulator for the AEOS runtime.

    Each "tick" represents one second of simulated time.
    Tasks arrive at task_arrival_rate/sec (Poisson), assigned via
    ExpectedUtilityDecisionEngine, executed with synthetic durations,
    and failures injected per worker_crash_probability.
    """

    def __init__(
        self,
        learning_engine: DefaultLearningEngine | None = None,
    ) -> None:
        self._learning = learning_engine or DefaultLearningEngine()
        self._engine = ExpectedUtilityDecisionEngine(self._learning)

    async def run(self, scenario: SimulationScenario) -> SimulationResult:
        rng = random.Random(_SEED)
        result = SimulationResult(scenario_id=scenario.scenario_id)

        workers = self._create_workers(scenario, rng)
        crashed: set[str] = set()
        total_latency = 0.0
        latencies: list[float] = []

        ticks = int(scenario.simulation_duration_seconds)
        for tick in range(ticks):
            # Fault injection — random worker crashes
            for w in workers:
                if (w.worker_id not in crashed
                        and rng.random() < scenario.worker_crash_probability):
                    crashed.add(w.worker_id)
                    w.health_score = 0.0

            # Autoscale: bring crashed workers back up probabilistically
            if scenario.enable_autoscaling and crashed:
                recovered = [wid for wid in list(crashed) if rng.random() < 0.1]
                for wid in recovered:
                    crashed.discard(wid)
                    for w in workers:
                        if w.worker_id == wid:
                            w.health_score = 1.0
                result.autoscale_events += len(recovered)

            # Arrive tasks (Poisson)
            n_tasks = _poisson(rng, scenario.task_arrival_rate)
            available = [w for w in workers if w.worker_id not in crashed and w.is_healthy]

            for _ in range(n_tasks):
                if not available:
                    result.total_tasks_failed += 1
                    continue

                req = TaskRequirements(
                    task_type="sim_task",
                    task_id=str(uuid.uuid4()),
                    workflow_id=scenario.scenario_id,
                )

                decision = await self._engine.decide(req, available)
                if not decision.worker_id:
                    result.total_tasks_failed += 1
                    continue

                # Simulate execution
                worker = next((w for w in available if w.worker_id == decision.worker_id), None)
                if worker is None:
                    result.total_tasks_failed += 1
                    continue

                latency = max(
                    worker.avg_latency_ms + rng.gauss(0, scenario.latency_jitter_ms),
                    1.0,
                )
                latencies.append(latency)
                total_latency += latency

                success = rng.random() > worker.historical_success_rate * 0.1  # mostly succeed
                if success:
                    result.total_tasks_executed += 1
                    wid = decision.worker_id
                    result.worker_utilization[wid] = result.worker_utilization.get(wid, 0.0) + 1
                else:
                    result.total_tasks_failed += 1

                result.timeline.append({
                    "tick": tick,
                    "task_id": req.task_id,
                    "worker_id": decision.worker_id,
                    "latency_ms": round(latency, 2),
                    "success": success,
                })

        # Aggregate
        n = result.total_tasks_executed
        if n > 0:
            result.avg_latency_ms = round(total_latency / n, 2)
        if latencies:
            latencies.sort()
            p99_idx = int(len(latencies) * 0.99)
            result.p99_latency_ms = round(latencies[min(p99_idx, len(latencies) - 1)], 2)

        elapsed = scenario.simulation_duration_seconds
        total = result.total_tasks_executed + result.total_tasks_failed
        result.throughput_rps = round(total / elapsed, 2) if elapsed > 0 else 0.0

        # Normalise worker utilisation to fraction of ticks
        for wid in result.worker_utilization:
            result.worker_utilization[wid] = round(
                result.worker_utilization[wid] / max(ticks, 1), 4
            )

        logger.info(
            "SimulationEngine: scenario=%s executed=%d failed=%d avg_lat=%.1fms",
            scenario.scenario_id,
            result.total_tasks_executed,
            result.total_tasks_failed,
            result.avg_latency_ms,
        )
        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _create_workers(
        scenario: SimulationScenario,
        rng: random.Random,
    ) -> list[CapabilityProfile]:
        workers = []
        regions = ["us-east-1", "us-west-2", "eu-west-1"]
        for i in range(scenario.n_workers):
            workers.append(CapabilityProfile(
                worker_id=f"sim-worker-{i:03d}",
                memory_gb=rng.choice([8.0, 16.0, 32.0]),
                gpu_available=rng.random() > 0.7,
                avg_latency_ms=rng.uniform(20, 200),
                trust_score=rng.uniform(0.7, 1.0),
                historical_success_rate=rng.uniform(0.85, 1.0),
                current_load=rng.uniform(0.0, 0.6),
                health_score=1.0,
                region=rng.choice(regions),
                az=rng.choice(["a", "b", "c"]),
                token_cost_per_k=rng.uniform(0.001, 0.05),
            ))
        return workers


def _poisson(rng: random.Random, lam: float) -> int:
    """Simple Poisson variate."""
    import math
    L = math.exp(-lam)
    k, p = 0, 1.0
    while p > L:
        k += 1
        p *= rng.random()
    return k - 1
