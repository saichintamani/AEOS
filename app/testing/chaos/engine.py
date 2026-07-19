"""
app/testing/chaos/engine.py

Chaos Experiment Engine — orchestrates fault injection experiments.

Each experiment follows the scientific method:
  1. Hypothesis: define what SHOULD happen under fault
  2. Steady State: verify system is healthy before injection
  3. Inject: apply the fault
  4. Observe: collect metrics and state during fault
  5. Recovery: wait for and verify recovery
  6. Verdict: pass if system behaved per hypothesis

Implements FIP §1 requirement: "Failure injection MUST be executable
in CI against staging."
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"  # Could not verify hypothesis
    ABORTED = "ABORTED"            # Pre-conditions not met; no fault injected


@dataclass
class SteadyStateProbe:
    """A probe that verifies steady state before/after an experiment."""
    name: str
    probe_fn: Callable[[], Awaitable[bool]]
    required: bool = True  # If True, experiment aborts if probe fails pre-injection


@dataclass
class ExperimentResult:
    """Complete result of one chaos experiment."""
    experiment_id: str
    fault_type: str
    hypothesis: str
    started_at: float
    ended_at: float
    verdict: Verdict

    # Phase results
    pre_steady_state_ok: bool = False
    fault_injected: bool = False
    post_steady_state_ok: bool = False

    # Observations during fault
    observations: list[dict[str, Any]] = field(default_factory=list)
    recovery_time_seconds: float | None = None
    recovery_path: str = ""

    # Detailed error if verdict is FAIL
    failure_reason: str = ""

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "fault_type": self.fault_type,
            "hypothesis": self.hypothesis,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "verdict": self.verdict.value,
            "pre_steady_state_ok": self.pre_steady_state_ok,
            "fault_injected": self.fault_injected,
            "post_steady_state_ok": self.post_steady_state_ok,
            "observations": self.observations,
            "recovery_time_seconds": self.recovery_time_seconds,
            "recovery_path": self.recovery_path,
            "failure_reason": self.failure_reason,
        }


@dataclass
class ChaosExperiment:
    """
    A fully specified chaos experiment.

    Attributes:
        name: Human-readable name (e.g., "redis-primary-crash")
        fault: The fault to inject (from faults.py)
        hypothesis: What SHOULD be true after fault injection and recovery
        steady_state_probes: Checks that confirm healthy state
        observation_interval: How often to collect observations (seconds)
        max_recovery_wait: Maximum time to wait for recovery (seconds)
        expected_rto: Expected recovery time (used in verdict)
    """
    name: str
    fault: Any  # BaseFault subclass
    hypothesis: str
    steady_state_probes: list[SteadyStateProbe] = field(default_factory=list)
    observation_interval: float = 5.0
    max_recovery_wait: float = 120.0
    expected_rto: float | None = None  # seconds; None = no RTO assertion


class ChaosEngine:
    """
    Orchestrates and records chaos experiments.

    Usage::

        engine = ChaosEngine(output_dir="reports/chaos")
        result = await engine.run(experiment)
        engine.save_report(result)
    """

    def __init__(self, output_dir: str = "reports/chaos") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

    async def run(self, experiment: ChaosExperiment) -> ExperimentResult:
        """Execute a single chaos experiment end-to-end."""
        experiment_id = f"{experiment.name}-{int(time.time())}"
        started_at = time.time()

        result = ExperimentResult(
            experiment_id=experiment_id,
            fault_type=type(experiment.fault).__name__,
            hypothesis=experiment.hypothesis,
            started_at=started_at,
            ended_at=started_at,
            verdict=Verdict.INCONCLUSIVE,
        )

        logger.info("=== CHAOS EXPERIMENT: %s ===", experiment.name)
        logger.info("Hypothesis: %s", experiment.hypothesis)

        try:
            # ── Phase 1: Verify steady state ──────────────────────────────
            logger.info("[Phase 1] Verifying steady state...")
            pre_ok = await self._check_steady_state(experiment.steady_state_probes)
            result.pre_steady_state_ok = pre_ok

            if not pre_ok:
                result.verdict = Verdict.ABORTED
                result.failure_reason = "Pre-injection steady state not met — experiment aborted"
                result.ended_at = time.time()
                self.save_report(result)
                return result

            # ── Phase 2: Inject fault ─────────────────────────────────────
            logger.info("[Phase 2] Injecting fault: %s", type(experiment.fault).__name__)
            try:
                await experiment.fault.inject()
                result.fault_injected = True
                logger.info("[Phase 2] Fault injected successfully")
            except Exception as exc:
                result.verdict = Verdict.ABORTED
                result.failure_reason = f"Fault injection failed: {exc}"
                result.ended_at = time.time()
                self.save_report(result)
                return result

            # ── Phase 3: Observe ──────────────────────────────────────────
            logger.info("[Phase 3] Observing system under fault...")
            obs_start = time.time()
            while time.time() - obs_start < experiment.max_recovery_wait:
                obs = await experiment.fault.observe()
                obs["timestamp"] = time.time()
                result.observations.append(obs)
                logger.info("[Phase 3] Observation: %s", obs)

                if obs.get("recovered", False):
                    result.recovery_time_seconds = time.time() - obs_start
                    logger.info("[Phase 3] Recovery detected in %.1fs", result.recovery_time_seconds)
                    break

                await asyncio.sleep(experiment.observation_interval)
            else:
                logger.warning("[Phase 3] System did not recover within %.1fs", experiment.max_recovery_wait)

            # ── Phase 4: Recover (clean up fault) ─────────────────────────
            logger.info("[Phase 4] Removing fault...")
            try:
                recovery_path = await experiment.fault.recover()
                result.recovery_path = recovery_path
            except Exception as exc:
                logger.error("[Phase 4] Fault recovery failed: %s", exc)

            # Allow extra time for system to stabilize
            await asyncio.sleep(experiment.observation_interval)

            # ── Phase 5: Verify steady state post-recovery ────────────────
            logger.info("[Phase 5] Verifying post-recovery steady state...")
            post_ok = await self._check_steady_state(experiment.steady_state_probes)
            result.post_steady_state_ok = post_ok

            # ── Phase 6: Verdict ──────────────────────────────────────────
            verdict = self._determine_verdict(experiment, result, post_ok)
            result.verdict = verdict
            logger.info("[Phase 6] Verdict: %s", verdict.value)

        except asyncio.CancelledError:
            result.verdict = Verdict.ABORTED
            result.failure_reason = "Experiment cancelled"
            try:
                await experiment.fault.recover()
            except Exception:  # noqa: BLE001
                pass

        except Exception as exc:  # noqa: BLE001
            result.verdict = Verdict.FAIL
            result.failure_reason = f"Unexpected exception: {exc}"
            try:
                await experiment.fault.recover()
            except Exception:  # noqa: BLE001
                pass

        finally:
            result.ended_at = time.time()
            self.save_report(result)

        return result

    async def run_suite(
        self, experiments: list[ChaosExperiment]
    ) -> list[ExperimentResult]:
        """Run a suite of experiments sequentially and return all results."""
        results = []
        for exp in experiments:
            result = await self.run(exp)
            results.append(result)
            # Cool-down between experiments
            await asyncio.sleep(5.0)
        self._save_suite_summary(results)
        return results

    def save_report(self, result: ExperimentResult) -> Path:
        """Save a single experiment result as JSON."""
        path = self._output_dir / f"{result.experiment_id}.json"
        path.write_text(
            json.dumps(result.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Chaos report saved: %s", path)
        return path

    async def _check_steady_state(self, probes: list[SteadyStateProbe]) -> bool:
        """Run all probes; return True if all required probes pass."""
        for probe in probes:
            try:
                ok = await asyncio.wait_for(probe.probe_fn(), timeout=10.0)
                if not ok and probe.required:
                    logger.warning("Steady state probe FAILED: %s", probe.name)
                    return False
                if ok:
                    logger.debug("Steady state probe OK: %s", probe.name)
            except asyncio.TimeoutError:
                logger.warning("Steady state probe TIMEOUT: %s", probe.name)
                if probe.required:
                    return False
        return True

    def _determine_verdict(
        self,
        experiment: ChaosExperiment,
        result: ExperimentResult,
        post_steady_ok: bool,
    ) -> Verdict:
        """Evaluate the verdict based on observations and hypothesis checks."""
        if not post_steady_ok:
            result.failure_reason = "System did not return to steady state after fault removal"
            return Verdict.FAIL

        if result.recovery_time_seconds is None:
            # Recovery not detected during observation window
            result.failure_reason = f"No recovery signal within {experiment.max_recovery_wait}s"
            return Verdict.FAIL

        if experiment.expected_rto is not None:
            if result.recovery_time_seconds > experiment.expected_rto:
                result.failure_reason = (
                    f"Recovery time {result.recovery_time_seconds:.1f}s exceeded "
                    f"expected RTO {experiment.expected_rto:.1f}s"
                )
                return Verdict.FAIL

        return Verdict.PASS

    def _save_suite_summary(self, results: list[ExperimentResult]) -> None:
        """Save a suite summary JSON."""
        summary = {
            "timestamp": time.time(),
            "total": len(results),
            "passed": sum(1 for r in results if r.verdict == Verdict.PASS),
            "failed": sum(1 for r in results if r.verdict == Verdict.FAIL),
            "aborted": sum(1 for r in results if r.verdict == Verdict.ABORTED),
            "experiments": [
                {
                    "id": r.experiment_id,
                    "fault": r.fault_type,
                    "verdict": r.verdict.value,
                    "rto": r.recovery_time_seconds,
                }
                for r in results
            ],
        }
        path = self._output_dir / f"suite-summary-{int(time.time())}.json"
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("Suite summary saved: %s", path)
