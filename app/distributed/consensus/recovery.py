"""
app/distributed/consensus/recovery.py

Raft Node Recovery — wires DurableLogStore + SnapshotStore into RaftNode
on restart.

The recovery sequence follows the Raft paper §7:
  1. Load latest snapshot (if any) and apply to state machine
  2. Replay WAL entries that come after the snapshot
  3. Restore current_term, voted_for, commit_index
  4. Rebuild volatile state (next_index, match_index)
  5. Resume as FOLLOWER regardless of pre-crash role

This module also provides:
  - RaftPersistence: high-level façade that RaftNode uses for all
    durable operations (replacing direct state mutation)
  - RecoveryResult: structured output of the recovery process

Design invariants:
  - After recovery, the recovered state is indistinguishable from
    a node that never crashed (modulo in-flight RPCs that were lost)
  - Recovery never changes committed entries — only uncommitted
    entries that were not replicated to a majority are dropped
  - If WAL is corrupt, recovery halts at the last valid record
    (safe — committed entries are always in a majority of nodes)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .log_store import DurableLogStore, LogEntry, RecoveredState
from .snapshot_store import Snapshot, SnapshotStore

logger = logging.getLogger(__name__)


@dataclass
class RecoveryResult:
    """Structured result of RaftNode recovery."""
    success: bool
    recovered_at: float

    # Restored persistent state
    current_term: int = 0
    voted_for: str | None = None
    commit_index: int = -1
    log_entries_recovered: int = 0

    # Snapshot info
    snapshot_applied: bool = False
    snapshot_last_index: int = -1
    snapshot_last_term: int = 0

    # Diagnostics
    wal_segments_replayed: int = 0
    recovery_duration_ms: float = 0.0
    failure_reason: str = ""

    def __str__(self) -> str:
        if not self.success:
            return f"RecoveryResult(FAILED: {self.failure_reason})"
        return (
            f"RecoveryResult(term={self.current_term}, "
            f"commit={self.commit_index}, "
            f"entries={self.log_entries_recovered}, "
            f"snapshot={'yes' if self.snapshot_applied else 'no'}, "
            f"duration={self.recovery_duration_ms:.1f}ms)"
        )


class RaftPersistence:
    """
    Façade over DurableLogStore + SnapshotStore.

    RaftNode uses this for all durable operations:
      - Persisting term and voted_for (before responding to any RPC)
      - Appending log entries (before responding to AppendEntries)
      - Persisting commit index (after quorum confirmation)
      - Taking and loading snapshots (log compaction)

    All writes are crash-safe (fsync before returning).

    Usage::

        persistence = RaftPersistence(data_dir="/var/lib/aeos/raft/node-1")

        # On restart:
        result = persistence.recover(apply_snapshot_fn=my_state_machine.restore)
        # RaftNode restores its state from result

        # During operation:
        persistence.save_term(new_term, voted_for=peer_id)
        persistence.append_entries([entry1, entry2])
        persistence.save_commit(new_commit_index)

        # Snapshot:
        persistence.save_snapshot(state_machine_state, last_index, last_term)
    """

    def __init__(
        self,
        data_dir: str,
        node_id: str = "",
        cluster_id: str = "",
    ) -> None:
        self._data_dir = data_dir
        self._node_id = node_id
        self._cluster_id = cluster_id
        self._log_store = DurableLogStore(data_dir)
        self._snap_store = SnapshotStore(data_dir)
        self._open = False

    def recover(
        self,
        apply_snapshot_fn: Any = None,
    ) -> RecoveryResult:
        """
        Full recovery sequence. Must be called before any other operation.

        apply_snapshot_fn: optional callable(state_dict) invoked when a
            snapshot is loaded. If None, snapshot state is returned in
            RecoveryResult but not applied to any external state machine.
        """
        start = time.monotonic()
        self._snap_store.open()

        result = RecoveryResult(success=False, recovered_at=time.time())

        try:
            # Phase 1: Load snapshot
            latest_snap: Snapshot | None = self._snap_store.load_latest()
            if latest_snap is not None:
                result.snapshot_applied = True
                result.snapshot_last_index = latest_snap.last_included_index
                result.snapshot_last_term = latest_snap.last_included_term
                logger.info(
                    "Recovery: applying snapshot (index=%d term=%d)",
                    latest_snap.last_included_index, latest_snap.last_included_term,
                )
                if apply_snapshot_fn is not None:
                    apply_snapshot_fn(latest_snap.state)

            # Phase 2: Replay WAL
            recovered: RecoveredState = self._log_store.recover()
            result.current_term = recovered.current_term
            result.voted_for = recovered.voted_for
            result.commit_index = recovered.commit_index
            result.log_entries_recovered = len(recovered.log)

            # Phase 3: Mark success
            self._open = True
            result.success = True
            result.recovery_duration_ms = (time.monotonic() - start) * 1000

            logger.info("Raft recovery complete: %s", result)
            return result

        except Exception as exc:
            result.failure_reason = str(exc)
            result.recovery_duration_ms = (time.monotonic() - start) * 1000
            logger.error("Raft recovery FAILED: %s", exc)
            raise

    def open_fresh(self) -> None:
        """Open for a brand-new node (no prior state)."""
        self._snap_store.open()
        self._log_store.open()
        self._open = True

    def close(self) -> None:
        if self._open:
            self._log_store.close()
            self._open = False

    # ── Term and vote ──────────────────────────────────────────────────────

    def save_term(self, term: int, voted_for: str | None) -> None:
        """
        Durably persist term + voted_for.

        MUST be called before responding to any RequestVote or
        AppendEntries RPC that causes a term change.
        Violating this ordering breaks INV-RAFT-001.
        """
        self._log_store.save_term(term, voted_for)

    @property
    def current_term(self) -> int:
        return self._log_store.current_term

    @property
    def voted_for(self) -> str | None:
        return self._log_store.voted_for

    # ── Log ───────────────────────────────────────────────────────────────

    def append_entry(self, index: int, term: int, command: dict[str, Any]) -> LogEntry:
        """Durably append a log entry."""
        entry = LogEntry(term=term, index=index, command=command)
        self._log_store.append(entry)
        return entry

    def append_entries(self, entries: list[dict[str, Any]]) -> list[LogEntry]:
        """Durably append multiple entries from AppendEntries RPC."""
        result = []
        for e in entries:
            entry = LogEntry(
                term=e["term"],
                index=e["index"],
                command=e.get("command", {}),
            )
            self._log_store.append(entry)
            result.append(entry)
        return result

    def truncate_log_from(self, from_index: int) -> None:
        """Truncate conflicting entries from from_index."""
        self._log_store.truncate_from(from_index)

    def get_entry(self, index: int) -> LogEntry | None:
        return self._log_store.get(index)

    def get_entries_from(self, from_index: int) -> list[LogEntry]:
        last = self._log_store.last_index()
        if from_index > last:
            return []
        return self._log_store.get_range(from_index, last)

    def last_log_index(self) -> int:
        return self._log_store.last_index()

    def last_log_term(self) -> int:
        return self._log_store.last_term()

    def log_length(self) -> int:
        return len(self._log_store)

    # ── Commit ────────────────────────────────────────────────────────────

    def save_commit(self, commit_index: int) -> None:
        """Durably persist the new commit index."""
        self._log_store.save_commit_index(commit_index)

    @property
    def commit_index(self) -> int:
        return self._log_store.commit_index

    # ── Snapshots ─────────────────────────────────────────────────────────

    def save_snapshot(
        self,
        state: dict[str, Any],
        last_included_index: int,
        last_included_term: int,
    ) -> None:
        """
        Save a state machine snapshot and compact the WAL.

        Steps:
          1. Write snapshot file (atomic)
          2. Record snapshot pointer in WAL
          3. Delete WAL segments fully covered by snapshot
        """
        snap = self._snap_store.save(
            state=state,
            last_included_index=last_included_index,
            last_included_term=last_included_term,
            node_id=self._node_id,
            cluster_id=self._cluster_id,
        )
        self._log_store.compact_to_snapshot(
            last_included_index,
            last_included_term,
            str(snap.meta.sha256),  # Store sha256 as the snapshot path reference
        )

    def load_latest_snapshot(self) -> Snapshot | None:
        return self._snap_store.load_latest()


def integrate_with_raft_node(raft_node: Any, persistence: RaftPersistence) -> None:
    """
    Wire RaftPersistence into an existing RaftNode instance.

    Replaces RaftNode's in-memory state mutations with durable equivalents.
    Called once during startup after recovery.

    This is a thin integration shim — it patches the specific methods
    that mutate persistent Raft state in the existing RaftNode implementation.
    """
    original_propose = raft_node.propose.__func__

    async def durable_propose(self_node: Any, command: dict) -> bool:
        from app.distributed.consensus.raft import RaftRole
        if self_node._role != RaftRole.LEADER:
            return False
        index = len(self_node._state.log)
        # Persist to WAL first
        entry = persistence.append_entry(
            index=index,
            term=self_node._state.current_term,
            command=command,
        )
        # Also update in-memory state
        from app.distributed.consensus.raft import LogEntry as RaftLogEntry
        self_node._state.log.append(
            RaftLogEntry(term=entry.term, index=entry.index, command=entry.command)
        )
        await self_node._replicate()
        return True

    def durable_save_term(term: int, voted_for: str | None) -> None:
        persistence.save_term(term, voted_for)
        raft_node._state.current_term = term
        raft_node._state.voted_for = voted_for

    # Attach as attributes so RaftNode can call them
    raft_node._persist = persistence
    raft_node._durable_save_term = durable_save_term
    raft_node.propose = lambda command: durable_propose(raft_node, command)

    logger.info(
        "RaftNode %s: durable persistence integrated (WAL dir=%s)",
        getattr(raft_node, "_id", "?"), persistence._data_dir,
    )
