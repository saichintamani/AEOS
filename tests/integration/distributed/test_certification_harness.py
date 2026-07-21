"""
Phase 13 Sprint 5 — certification harness tests.

The single most important property under test is the HONESTY GATE:
a non-production-grade environment (e.g. a developer workstation) can NEVER emit
``certified=True``, no matter how good the measured numbers are or whether
``--allow-full-scale`` was requested. If that ever regresses, the harness would
start manufacturing production claims — exactly what the sprint forbids.

We also verify:
  - environment classification is honest on the host running the suite;
  - the scale gate never selects full-scale off production-grade infra;
  - the threshold evaluator passes/fails on the right conditions;
  - a real Bronze dev-scale run produces REAL, non-empty measurements with no
    errors on the throughput / recovery / federation paths.

Marked to require grpc + cryptography (the real measurements dispatch RPCs and
sign tokens).
"""

from __future__ import annotations

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

from app.certification import CertificationHarness, Tier, classify
from app.certification.environment import Environment, EnvironmentClass
from app.certification.profiles import get_profile
from app.certification.runner import _evaluate


def _fake_env(cls: EnvironmentClass) -> Environment:
    return Environment(
        environment_class=cls, hostname="test", os_name="Linux",
        os_release="test", platform="test", python_version="3.13",
        cpu_count=8, logical_cpus=8, total_memory_mb=16000.0,
        in_container=False, in_kubernetes=(cls is EnvironmentClass.KUBERNETES),
        is_ci=False, signals=["synthetic"],
    )


def test_environment_classification_is_honest():
    env = classify()
    # Whatever host runs this, the class must be a real member and the
    # production-grade flag must agree with the class.
    assert isinstance(env.environment_class, EnvironmentClass)
    if env.environment_class in (EnvironmentClass.DEVELOPER_WORKSTATION,
                                 EnvironmentClass.CI, EnvironmentClass.UNKNOWN):
        assert env.is_production_grade is False


def test_scale_gate_never_selects_full_scale_off_production():
    """allow_full_scale=True on a developer workstation still runs dev-scale."""
    dev_env = _fake_env(EnvironmentClass.DEVELOPER_WORKSTATION)
    harness = CertificationHarness(allow_full_scale=True, environment=dev_env)
    profile = get_profile(Tier.BRONZE)
    scale, is_full = harness._choose_scale(profile)  # noqa: SLF001
    assert is_full is False
    assert scale.label == "dev-scale"


def test_scale_gate_selects_full_scale_only_on_production_with_optin():
    prod_env = _fake_env(EnvironmentClass.KUBERNETES)
    # Without opt-in → still dev-scale.
    h1 = CertificationHarness(allow_full_scale=False, environment=prod_env)
    _, is_full1 = h1._choose_scale(get_profile(Tier.BRONZE))  # noqa: SLF001
    assert is_full1 is False
    # With opt-in on production infra → full-scale.
    h2 = CertificationHarness(allow_full_scale=True, environment=prod_env)
    scale2, is_full2 = h2._choose_scale(get_profile(Tier.BRONZE))  # noqa: SLF001
    assert is_full2 is True
    assert scale2.label == "full-scale"


def test_threshold_evaluator_pass_and_fail():
    th = get_profile(Tier.BRONZE).thresholds
    good = {
        "throughput_latency": {"scalar": {"throughput_tps": th.min_throughput_tps + 10},
                                "stats": {"p99": th.max_p99_latency_ms - 1},
                                "error_rate": 0.0},
        "failover": {"stats": {"p99": th.max_failover_ms - 1}, "error_count": 0},
        "recovery": {"stats": {"p99": th.max_recovery_ms - 1}, "error_count": 0},
    }
    met, failures = _evaluate(good, th)
    assert met is True and failures == []

    bad = {
        "throughput_latency": {"scalar": {"throughput_tps": 1.0},
                                "stats": {"p99": th.max_p99_latency_ms + 1000},
                                "error_rate": 1.0},
        "failover": {"stats": {"p99": th.max_failover_ms + 1000}, "error_count": 2},
        "recovery": {"stats": {"p99": th.max_recovery_ms + 1000}, "error_count": 1},
    }
    met2, failures2 = _evaluate(bad, th)
    assert met2 is False and len(failures2) >= 4


@pytest.mark.asyncio
async def test_real_bronze_dev_scale_run_is_honest_and_measured():
    """A real Bronze dev-scale run: never certified on a dev box, but produces
    real, non-empty, error-free measurements on the deterministic paths."""
    env = _fake_env(EnvironmentClass.DEVELOPER_WORKSTATION)
    harness = CertificationHarness(allow_full_scale=True, environment=env)
    result = await harness.run_tier(Tier.BRONZE)

    # Honesty gate: dev box → never certified, with explicit blocking reasons.
    assert result.certified is False
    assert result.scale_is_full is False
    assert any("not production-grade" in r for r in result.certified_blocked_reasons)

    # Real measurements present on every dimension.
    for dim in ("throughput_latency", "failover", "recovery", "federation_overhead"):
        assert dim in result.measurements

    # Deterministic paths must complete without errors and yield samples.
    tput = result.measurements["throughput_latency"]
    assert tput["sample_count"] > 0
    assert tput["error_count"] == 0
    assert tput["scalar"]["throughput_tps"] > 0

    rec = result.measurements["recovery"]
    assert rec["sample_count"] > 0 and rec["error_count"] == 0

    fed = result.measurements["federation_overhead"]
    assert fed["sample_count"] > 0 and fed["error_count"] == 0
