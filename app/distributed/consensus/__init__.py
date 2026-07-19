"""app.distributed.consensus — Raft consensus implementation with WAL persistence."""

from .wal import WriteAheadLog, WALRecord, WALSegment, WALCorruption, WALTruncated
from .log_store import DurableLogStore, LogEntry as DurableLogEntry, RecoveredState
from .snapshot_store import SnapshotStore, Snapshot, SnapshotMeta, SnapshotCorruption
from .recovery import RaftPersistence, RecoveryResult, integrate_with_raft_node

__all__ = [
    # WAL layer
    "WriteAheadLog", "WALRecord", "WALSegment", "WALCorruption", "WALTruncated",
    # Log store
    "DurableLogStore", "DurableLogEntry", "RecoveredState",
    # Snapshot store
    "SnapshotStore", "Snapshot", "SnapshotMeta", "SnapshotCorruption",
    # Recovery façade
    "RaftPersistence", "RecoveryResult", "integrate_with_raft_node",
]
