"""
app/certification/profiles.py

Certification profiles: Bronze / Silver / Gold / Platinum.

Each profile carries two scale settings for the SAME set of measurements:

  * ``full_scale``  — the real target load a tier certifies at. Running this is
    only meaningful (and only permitted to yield ``certified=True``) on a
    production-grade environment with an explicit opt-in flag.
  * ``dev_scale``   — a small, fast subset that exercises the identical code
    paths on a developer workstation. It produces REAL measurements, but the
    runner always reports ``certified=False`` for a dev-scale run.

The thresholds (throughput floor, P99 latency ceiling, failover/recovery RTO,
error-rate ceiling) are the tier's pass criteria. They are checked against
whatever scale actually ran; the report records which scale was used so a
dev-scale "meets thresholds" is never confused with a certification.

Design intent: the SAME command (`scripts/certify.py <tier>`) runs on a laptop,
a Linux server, and Kubernetes. Only the environment classification and the
`--allow-full-scale` opt-in change what scale runs and whether a certification
claim is legitimate — the measurement code is identical everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .environment import EnvironmentClass


class Tier(str, Enum):
    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"
    PLATINUM = "platinum"


@dataclass(frozen=True)
class ScaleSettings:
    """Load parameters for one scale of a tier's measurements."""

    label: str                     # "full-scale" | "dev-scale"
    throughput_tasks: int          # tasks submitted for the throughput/latency run
    throughput_concurrency: int    # in-flight concurrent submissions
    failover_nodes: int            # Raft cluster size for the failover measurement
    failover_trials: int           # number of leader-kill trials
    recovery_checkpoints: int      # checkpoints written+recovered for the recovery run
    federation_samples: int        # federated round-trips sampled


@dataclass(frozen=True)
class Thresholds:
    """Tier pass criteria. A run PASSES a tier only if every applicable
    threshold is met AND the run was executed at full scale on a certifiable
    environment (the latter two gates live in the runner)."""

    min_throughput_tps: float
    max_p99_latency_ms: float
    max_error_rate: float
    max_failover_ms: float
    max_recovery_ms: float


@dataclass(frozen=True)
class Profile:
    tier: Tier
    description: str
    full_scale: ScaleSettings
    dev_scale: ScaleSettings
    thresholds: Thresholds
    certifiable_in: frozenset  # EnvironmentClass values where certified=True is legitimate


# Environments where a genuine certification claim is legitimate for any tier.
_PROD_ENVS = frozenset({
    EnvironmentClass.LINUX_SERVER,
    EnvironmentClass.KUBERNETES,
    EnvironmentClass.CLOUD,
})


PROFILES: dict[Tier, Profile] = {
    Tier.BRONZE: Profile(
        tier=Tier.BRONZE,
        description="Entry tier: single-node functional load, fast failover, "
                    "clean recovery. The floor for calling AEOS 'operational'.",
        full_scale=ScaleSettings(
            label="full-scale",
            throughput_tasks=10_000, throughput_concurrency=32,
            failover_nodes=3, failover_trials=5,
            recovery_checkpoints=50, federation_samples=50,
        ),
        dev_scale=ScaleSettings(
            label="dev-scale",
            throughput_tasks=500, throughput_concurrency=16,
            failover_nodes=3, failover_trials=3,
            recovery_checkpoints=10, federation_samples=20,
        ),
        thresholds=Thresholds(
            min_throughput_tps=50.0, max_p99_latency_ms=2000.0,
            max_error_rate=0.01, max_failover_ms=3000.0, max_recovery_ms=3000.0,
        ),
        certifiable_in=_PROD_ENVS,
    ),
    Tier.SILVER: Profile(
        tier=Tier.SILVER,
        description="Sustained multi-node load with tighter latency and failover "
                    "budgets. Suitable for internal production workloads.",
        full_scale=ScaleSettings(
            label="full-scale",
            throughput_tasks=100_000, throughput_concurrency=64,
            failover_nodes=5, failover_trials=10,
            recovery_checkpoints=200, federation_samples=200,
        ),
        dev_scale=ScaleSettings(
            label="dev-scale",
            throughput_tasks=1_000, throughput_concurrency=24,
            failover_nodes=5, failover_trials=3,
            recovery_checkpoints=20, federation_samples=30,
        ),
        thresholds=Thresholds(
            min_throughput_tps=200.0, max_p99_latency_ms=1500.0,
            max_error_rate=0.005, max_failover_ms=2000.0, max_recovery_ms=2000.0,
        ),
        certifiable_in=_PROD_ENVS,
    ),
    Tier.GOLD: Profile(
        tier=Tier.GOLD,
        description="High-throughput, low-latency tier. Requires dedicated "
                    "multi-node infrastructure; NOT achievable on a workstation.",
        full_scale=ScaleSettings(
            label="full-scale",
            throughput_tasks=1_000_000, throughput_concurrency=128,
            failover_nodes=5, failover_trials=20,
            recovery_checkpoints=1_000, federation_samples=1_000,
        ),
        dev_scale=ScaleSettings(
            label="dev-scale",
            throughput_tasks=2_000, throughput_concurrency=32,
            failover_nodes=5, failover_trials=3,
            recovery_checkpoints=25, federation_samples=40,
        ),
        thresholds=Thresholds(
            min_throughput_tps=1000.0, max_p99_latency_ms=1000.0,
            max_error_rate=0.001, max_failover_ms=1500.0, max_recovery_ms=1500.0,
        ),
        certifiable_in=_PROD_ENVS,
    ),
    Tier.PLATINUM: Profile(
        tier=Tier.PLATINUM,
        description="Sustained high load with chaos injection over a long window. "
                    "Requires production infrastructure and a real chaos harness.",
        full_scale=ScaleSettings(
            label="full-scale",
            throughput_tasks=2_000_000, throughput_concurrency=256,
            failover_nodes=7, failover_trials=30,
            recovery_checkpoints=2_000, federation_samples=2_000,
        ),
        dev_scale=ScaleSettings(
            label="dev-scale",
            throughput_tasks=2_000, throughput_concurrency=32,
            failover_nodes=7, failover_trials=3,
            recovery_checkpoints=25, federation_samples=40,
        ),
        thresholds=Thresholds(
            min_throughput_tps=2000.0, max_p99_latency_ms=1000.0,
            max_error_rate=0.001, max_failover_ms=1500.0, max_recovery_ms=1500.0,
        ),
        certifiable_in=_PROD_ENVS,
    ),
}


def get_profile(tier: Tier) -> Profile:
    return PROFILES[tier]
