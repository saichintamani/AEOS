"""
AEOS Phase 9 — Distributed Runtime Platform (DRP)
Wave 9B.1: Distributed Communication & Control Plane
Wave 9B.2: Distributed Execution Runtime

Package layout::

    app/distributed/
    ├── contracts/      Interfaces (ABCs) only — no implementations
    ├── communication/  gRPC client/server framework + service discovery
    ├── transport/      Message transport (Kafka, in-memory, test)
    ├── cluster/        Node identity, membership primitives, heartbeat
    ├── events/         Distributed event envelope, routing, serialisation
    ├── coordination/   Lease manager, distributed clock, cluster metadata
    ├── execution/      SM-TASK, checkpoint, recovery, fencing tokens
    ├── scheduler/      6 strategies, DistributedScheduler
    ├── worker/         WorkerRuntime, HeartbeatService, GovernanceClient
    ├── pool/           WorkerPool, WorkerSnapshot
    ├── backpressure/   ThresholdPolicy, BackpressureEngine
    ├── metrics/        Counter, Gauge, Histogram, RuntimeMetricsCollector
    └── fault/          FaultInjector, 8 fault scenarios
"""

from app.distributed.contracts.transport import MessageTransport, TransportMessage
from app.distributed.contracts.cluster import (
    NodeIdentity,
    ClusterMemberState,
    MemberRecord,
    MembershipStore,
)
from app.distributed.contracts.events import (
    EventEnvelope,
    EventRouter,
    EventSerializer,
    EventPublisher,
    EventConsumer,
)
from app.distributed.contracts.coordination import (
    LeaseStore,
    DistributedClock,
    ClusterMetadata,
)
from app.distributed.contracts.communication import (
    ServiceEndpoint,
    ServiceDiscovery,
    RpcChannel,
)

# Wave 9B.2 — Execution Runtime
from app.distributed.execution.states import ExecutionState, validate_execution_transition
from app.distributed.execution.context import ExecutionContext, CheckpointData
from app.distributed.execution.lease import ExecutionLeaseManager, FencingToken, StaleFencingTokenError
from app.distributed.execution.checkpoint import CheckpointEngine, CheckpointStore, InMemoryCheckpointStore
from app.distributed.execution.recovery import RecoveryRuntime, RecoveryResult
from app.distributed.execution.engine import TaskExecutionEngine
from app.distributed.scheduler.contracts import WorkerView, SchedulingDecision, SchedulingStrategy
from app.distributed.scheduler.scheduler import DistributedScheduler
from app.distributed.scheduler.strategies import (
    RoundRobinStrategy,
    LeastLoadedStrategy,
    CapabilityAwareStrategy,
    PriorityAwareStrategy,
    LocalityAwareStrategy,
    AffinityStrategy,
)
from app.distributed.worker.runtime import WorkerRuntime
from app.distributed.worker.heartbeat import HeartbeatService
from app.distributed.worker.governance import GovernanceClient, TokenRevokedException
from app.distributed.pool.worker_pool import WorkerPool
from app.distributed.pool.metrics import WorkerSnapshot
from app.distributed.backpressure.engine import BackpressureEngine, BackpressureState
from app.distributed.backpressure.policy import ThresholdPolicy
from app.distributed.metrics.registry import Counter, Gauge, Histogram, MetricsRegistry
from app.distributed.metrics.collectors import RuntimeMetricsCollector
from app.distributed.fault.injector import FaultInjector, FaultType, VerificationResult

__all__ = [
    # Transport
    "MessageTransport",
    "TransportMessage",
    # Cluster
    "NodeIdentity",
    "ClusterMemberState",
    "MemberRecord",
    "MembershipStore",
    # Events
    "EventEnvelope",
    "EventRouter",
    "EventSerializer",
    "EventPublisher",
    "EventConsumer",
    # Coordination
    "LeaseStore",
    "DistributedClock",
    "ClusterMetadata",
    # Communication
    "ServiceEndpoint",
    "ServiceDiscovery",
    "RpcChannel",
    # Execution (Wave 9B.2)
    "ExecutionState",
    "validate_execution_transition",
    "ExecutionContext",
    "CheckpointData",
    "ExecutionLeaseManager",
    "FencingToken",
    "StaleFencingTokenError",
    "CheckpointEngine",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "RecoveryRuntime",
    "RecoveryResult",
    "TaskExecutionEngine",
    # Scheduler
    "WorkerView",
    "SchedulingDecision",
    "SchedulingStrategy",
    "DistributedScheduler",
    "RoundRobinStrategy",
    "LeastLoadedStrategy",
    "CapabilityAwareStrategy",
    "PriorityAwareStrategy",
    "LocalityAwareStrategy",
    "AffinityStrategy",
    # Worker
    "WorkerRuntime",
    "HeartbeatService",
    "GovernanceClient",
    "TokenRevokedException",
    # Pool
    "WorkerPool",
    "WorkerSnapshot",
    # Backpressure
    "BackpressureEngine",
    "BackpressureState",
    "ThresholdPolicy",
    # Metrics
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "RuntimeMetricsCollector",
    # Fault injection
    "FaultInjector",
    "FaultType",
    "VerificationResult",
]
