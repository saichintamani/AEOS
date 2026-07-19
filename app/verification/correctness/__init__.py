"""
app/verification/correctness/__init__.py
Correctness Validation Framework for AEOS.

Provides:
- InvariantValidator     — 100% INV-* coverage (extends distributed/validation)
- ProtocolValidator      — full PROTO-* trace validation (19 protocols)
- ReplayValidator        — execution trace replay for correctness proof
- StateMachineValidator  — live event stream wiring (8 machines)
- ClusterConsistencyValidator — 3-view membership comparison
"""

from .invariant_validator import InvariantValidator, InvariantCoverageReport
from .protocol_validator import FullProtocolValidator, ProtocolCoverageReport
from .replay_validator import ReplayValidator, ReplayResult
from .state_machine_validator import LiveStateMachineValidator
from .cluster_consistency_validator import ClusterConsistencyValidator, ConsistencyReport

__all__ = [
    "InvariantValidator",
    "InvariantCoverageReport",
    "FullProtocolValidator",
    "ProtocolCoverageReport",
    "ReplayValidator",
    "ReplayResult",
    "LiveStateMachineValidator",
    "ClusterConsistencyValidator",
    "ConsistencyReport",
]
