"""Fault injection framework for invariant verification."""

from app.distributed.fault.injector import FaultInjector, FaultType, VerificationResult
from app.distributed.fault.scenarios import (
    WorkerCrashScenario,
    LeaseExpirationScenario,
    NetworkDelayScenario,
    DuplicateEventScenario,
    CheckpointCorruptionScenario,
    SlowWorkerScenario,
    HeartbeatLossScenario,
    ClockSkewScenario,
)

__all__ = [
    "FaultInjector",
    "FaultType",
    "VerificationResult",
    "WorkerCrashScenario",
    "LeaseExpirationScenario",
    "NetworkDelayScenario",
    "DuplicateEventScenario",
    "CheckpointCorruptionScenario",
    "SlowWorkerScenario",
    "HeartbeatLossScenario",
    "ClockSkewScenario",
]
