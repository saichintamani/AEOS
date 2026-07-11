"""Distributed execution: state machine, context, lease, checkpoint, recovery, engine."""

from app.distributed.execution.states import ExecutionState, VALID_EXECUTION_TRANSITIONS, validate_execution_transition
from app.distributed.execution.context import ExecutionContext, CheckpointData
from app.distributed.execution.lease import ExecutionLeaseManager, FencingToken, StaleFencingTokenError
from app.distributed.execution.checkpoint import CheckpointEngine, CheckpointStore, InMemoryCheckpointStore, CheckpointType
from app.distributed.execution.recovery import RecoveryRuntime, RecoveryResult
from app.distributed.execution.engine import TaskExecutionEngine, ExecutionCallbacks

__all__ = [
    "ExecutionState",
    "VALID_EXECUTION_TRANSITIONS",
    "validate_execution_transition",
    "ExecutionContext",
    "CheckpointData",
    "ExecutionLeaseManager",
    "FencingToken",
    "StaleFencingTokenError",
    "CheckpointEngine",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "CheckpointType",
    "RecoveryRuntime",
    "RecoveryResult",
    "TaskExecutionEngine",
    "ExecutionCallbacks",
]
