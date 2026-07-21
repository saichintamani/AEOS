"""
app/certification/runner.py

The certification harness: runs all five measurements for a tier at the
appropriate scale, evaluates them against the tier thresholds, and — critically
— decides whether the run may legitimately claim a certification.

The gating rule is deliberately strict and lives in ONE place:

    certified == True  ⇔  (a) the environment class is one where the tier is
                              certifiable (production-grade), AND
                          (b) the run executed at FULL scale, AND
                          (c) full-scale was explicitly opted into, AND
                          (d) every applicable threshold was met.

On a developer workstation, (a) and (c) are false, so ``certified`` is always
False no matter how good the numbers look. The run still produces real
measurements and a ``thresholds_met`` boolean — the report simply labels it an
*operational validation at dev-scale*, not a certification. This is the
mechanism that makes "no fabricated production claims" structural rather than a
promise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from . import measurements as M
from .environment import Environment, classify
from .profiles import Profile, ScaleSettings, Thresholds, Tier, get_profile


@dataclass
class TierRunResult:
    tier: Tier
    run_id: str
    started_at: float
    ended_at: float
    environment: Environment
    scale: ScaleSettings
    scale_is_full: bool
    measurements: dict[str, dict[str, Any]] = field(default_factory=dict)
    thresholds_met: bool = False
    threshold_failures: list[str] = field(default_factory=list)
    certified: bool = False
    certified_blocked_reasons: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        return self.ended_at - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "tier": self.tier.value,
            "run_id": self.run_id,
            "started_at_epoch": self.started_at,
            "ended_at_epoch": self.ended_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "environment": self.environment.to_dict(),
            "scale_profile": self.scale.label,
            "scale_is_full": self.scale_is_full,
            "measurements": self.measurements,
            "thresholds_met": self.thresholds_met,
            "threshold_failures": self.threshold_failures,
            "certified": self.certified,
            "certified_blocked_reasons": self.certified_blocked_reasons,
            "disclaimer": self._disclaimer(),
        }

    def _disclaimer(self) -> str:
        if self.certified:
            return (f"CERTIFIED {self.tier.value.upper()} on "
                    f"{self.environment.environment_class.value} at full scale.")
        return (
            "NOT A CERTIFICATION. These are real measurements of real AEOS "
            f"operations at {self.scale.label} on "
            f"'{self.environment.environment_class.value}'. "
            "A tier certification requires a production-grade environment, a "
            "full-scale run, and the explicit --allow-full-scale opt-in. "
            + ("Thresholds were met at this scale, but that does not constitute "
               "a production certification." if self.thresholds_met else
               "Thresholds were not all met at this scale.")
        )


def _evaluate(measurements: dict[str, dict[str, Any]],
              th: Thresholds) -> tuple[bool, list[str]]:
    """Check real measurements against tier thresholds. Returns (met, failures)."""
    failures: list[str] = []

    tput = measurements.get("throughput_latency", {})
    tps = tput.get("scalar", {}).get("throughput_tps", 0.0)
    if tps < th.min_throughput_tps:
        failures.append(
            f"throughput {tps:.1f} tps < required {th.min_throughput_tps:.0f} tps")
    p99 = tput.get("stats", {}).get("p99", float("inf"))
    if p99 > th.max_p99_latency_ms:
        failures.append(
            f"schedule P99 {p99:.1f}ms > limit {th.max_p99_latency_ms:.0f}ms")
    err = tput.get("error_rate", 1.0)
    if err > th.max_error_rate:
        failures.append(
            f"error rate {err:.4f} > limit {th.max_error_rate:.4f}")

    fo = measurements.get("failover", {})
    fo_p99 = fo.get("stats", {}).get("p99", float("inf"))
    if fo_p99 > th.max_failover_ms:
        failures.append(
            f"failover P99 {fo_p99:.1f}ms > limit {th.max_failover_ms:.0f}ms")
    if fo.get("error_count", 1) > 0:
        failures.append(f"failover had {fo['error_count']} failed re-election(s)")

    rec = measurements.get("recovery", {})
    rec_p99 = rec.get("stats", {}).get("p99", float("inf"))
    if rec_p99 > th.max_recovery_ms:
        failures.append(
            f"recovery P99 {rec_p99:.1f}ms > limit {th.max_recovery_ms:.0f}ms")
    if rec.get("error_count", 1) > 0:
        failures.append(f"recovery had {rec['error_count']} failed read-back(s)")

    return (len(failures) == 0), failures


class CertificationHarness:
    """Runs a tier's measurements and applies the certification gate."""

    def __init__(self, *, allow_full_scale: bool = False,
                 environment: Environment | None = None) -> None:
        self._allow_full_scale = allow_full_scale
        self._env = environment or classify()

    def _choose_scale(self, profile: Profile) -> tuple[ScaleSettings, bool]:
        """Full scale only when explicitly opted in AND the environment is
        production-grade. Otherwise dev scale."""
        if self._allow_full_scale and self._env.is_production_grade:
            return profile.full_scale, True
        return profile.dev_scale, False

    async def run_tier(self, tier: Tier) -> TierRunResult:
        profile = get_profile(tier)
        scale, is_full = self._choose_scale(profile)
        run_id = f"{tier.value}-{int(time.time())}"
        started = time.time()

        result = TierRunResult(
            tier=tier, run_id=run_id, started_at=started, ended_at=started,
            environment=self._env, scale=scale, scale_is_full=is_full,
        )

        # Real measurements — each drives a real AEOS seam.
        for name, fn in (
            ("throughput_latency", M.measure_throughput_latency),
            ("failover", M.measure_failover),
            ("recovery", M.measure_recovery),
            ("federation_overhead", M.measure_federation_overhead),
        ):
            mres = await fn(scale)
            result.measurements[name] = mres.to_dict()

        result.ended_at = time.time()

        # Threshold evaluation (on whatever scale actually ran).
        met, failures = _evaluate(result.measurements, profile.thresholds)
        result.thresholds_met = met
        result.threshold_failures = failures

        # Certification gate — strict, single source of truth.
        blocked: list[str] = []
        if self._env.environment_class not in profile.certifiable_in:
            blocked.append(
                f"environment '{self._env.environment_class.value}' is not "
                f"production-grade; {tier.value} is certifiable only on "
                f"{sorted(c.value for c in profile.certifiable_in)}")
        if not is_full:
            blocked.append("run executed at dev-scale, not full-scale")
        if not self._allow_full_scale:
            blocked.append("--allow-full-scale was not set")
        if not met:
            blocked.append("one or more thresholds not met")

        result.certified = len(blocked) == 0
        result.certified_blocked_reasons = blocked
        return result
