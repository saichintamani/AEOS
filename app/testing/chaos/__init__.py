"""
app/testing/chaos/__init__.py
Production-grade Chaos Engineering Platform for AEOS.

Implements all 12 fault types from docs/verification/020-FAILURE_INJECTION_PLAN.md.
Every experiment has: hypothesis, execution, observation, recovery, pass/fail.
"""

from .engine import ChaosEngine, ChaosExperiment, ExperimentResult
from .faults import (
    NodeCrashFault,
    ProcessKillFault,
    NetworkPartitionFault,
    SplitBrainFault,
    KafkaBrokerLossFault,
    RedisShardLossFault,
    CheckpointCorruptionFault,
    ClockSkewFault,
    SchedulerCrashFault,
    GovernanceOutageFault,
    StorageExhaustionFault,
    MemoryPressureFault,
)

__all__ = [
    "ChaosEngine",
    "ChaosExperiment",
    "ExperimentResult",
    "NodeCrashFault",
    "ProcessKillFault",
    "NetworkPartitionFault",
    "SplitBrainFault",
    "KafkaBrokerLossFault",
    "RedisShardLossFault",
    "CheckpointCorruptionFault",
    "ClockSkewFault",
    "SchedulerCrashFault",
    "GovernanceOutageFault",
    "StorageExhaustionFault",
    "MemoryPressureFault",
]
