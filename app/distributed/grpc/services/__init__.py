"""
AEOS domain gRPC servicers — the wire realization of the five service contracts
defined under proto/aeos/**:

  - GovernanceServiceServicer   (aeos.governance.v1)  — token mint/verify/revoke
  - SchedulerServiceServicer    (aeos.core.v1)        — task schedule/cancel/status
  - WorkerServiceServicer       (aeos.core.v1)        — fleet register/heartbeat
  - ObservabilityServiceServicer(aeos.observability.v1) — spans/events ingest
  - FederationServiceServicer   (aeos.federation.v1)  — cross-cluster dispatch

``DomainServiceServer`` hosts any subset of them on a single grpc.aio server,
mirroring the lifecycle of GrpcEventBusTransport.

Phase: 13 Sprint 3
"""

from __future__ import annotations

from .federation_executor import (
    EVIDENCE_AUDIENCE,
    FederatedExecutor,
    FederationClient,
    FederationTrustError,
    extract_jti,
    make_echo_executor,
)
from .federation_service import FederationServiceServicer
from .governance_service import GovernanceServiceServicer
from .observability_service import ObservabilityServiceServicer
from .scheduler_service import SchedulerServiceServicer
from .server import DomainServiceServer
from .worker_service import WorkerServiceServicer

__all__ = [
    "GovernanceServiceServicer",
    "SchedulerServiceServicer",
    "WorkerServiceServicer",
    "ObservabilityServiceServicer",
    "FederationServiceServicer",
    "DomainServiceServer",
    # Remote federated execution (Sprint 4)
    "FederatedExecutor",
    "FederationClient",
    "FederationTrustError",
    "make_echo_executor",
    "extract_jti",
    "EVIDENCE_AUDIENCE",
]
