"""
app/distributed/consensus/log_store.py

Durable Log Store — sits between RaftNode and the WAL.

Provides the interface RaftNode expects (in-memory list semantics)
while persisting every mutation to the WAL before returning.

This is the "storage abstraction" layer from the Raft paper §7:
  "Servers store log entries on stable storage before responding to RPCs."

Design:
  - In-memory index for O(1) index lookups (log[i])
  - WAL write before any in-memory mutation (write-ahead guarantee)
  - Crash recovery rebuilds in-memory state from WAL on startup
"""

from __future__ import annotations

import logging
from typing import Any

from .wal import WriteAheadLog

logger = logging.getLogger(__name__)


class LogEntry:
    """Mirrors app/distributed/consensus/raft.py:LogEntry for store use."""
    __slots__ = ("term", "index", "command", "committed")

    def __init__(self, term: int, index: int, command: dict[str, Any],
                 committed: bool = False) -> None:
        self.term = term
        self.index = index
        self.command = command
        self.committed = committed

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "term": self.term, "command": self.command}


class DurableLogStore:
    """
    WAL-backed Raft log store.

    All mutations are written to the WAL with fsync before being
    applied to the in-memory log. On restart, call recover() to
    rebuild state from the WAL.

    Usage (normal startup)::

        store = DurableLogStore(wal_dir="/var/lib/aeos/raft/node-1")
        store.open()
        # ... use store as Raft log

    Usage (crash recovery)::

        store = DurableLogStore(wal_dir="/var/lib/aeos/raft/node-1")
        recovered = store.recover()
        # recovered.current_term, recovered.voted_for, recovered.commit_index
        # store._log now populated with recovered entries

    Invariants:
        - len(self._log) == 0 OR self._log[0].index == 0  (if no snapshot)
        - self._log is sorted by index with no gaps
        - Every entry in self._log exists in the WAL
    """

    def __init__(self, wal_dir: str) -> None:
        self._wal = WriteAheadLog(wal_dir)
        self._log: list[LogEntry] = []
        self._current_term: int = 0
        self._voted_for: str | None = None
        self._commit_index: int = -1
        self._snapshot_last_index: int = -1
        self._snapshot_last_term: int = 0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def open(self) -> None:
        """Open WAL for writing without recovery (fresh start)."""
        self._wal.open()

    def recover(self) -> "RecoveredState":
        """
        Replay WAL and return recovered persistent state.
        Must be called before open() on a restart.
        """
        raw = self._wal.recover()

        self._current_term = raw["current_term"]
        self._voted_for = raw["voted_for"]
        self._commit_index = raw["commit_index"]

        # Rebuild in-memory log
        self._log = [
            LogEntry(
                term=e["term"],
                index=e["index"],
                command=e["command"],
                committed=e["index"] <= self._commit_index,
            )
            for e in raw["entries"]
        ]

        # Track snapshot boundary
        if raw["snapshot_ptr"]:
            self._snapshot_last_index = raw["snapshot_ptr"]["last_included_index"]
            self._snapshot_last_term = raw["snapshot_ptr"]["last_included_term"]

        logger.info(
            "DurableLogStore recovered: term=%d voted_for=%s commit_index=%d entries=%d",
            self._current_term, self._voted_for, self._commit_index, len(self._log),
        )

        # Open WAL for further appends
        self._wal.open()

        return RecoveredState(
            current_term=self._current_term,
            voted_for=self._voted_for,
            commit_index=self._commit_index,
            log=self._log,
            snapshot_last_index=self._snapshot_last_index,
            snapshot_last_term=self._snapshot_last_term,
        )

    def close(self) -> None:
        self._wal.close()

    # ── Term + Vote persistence ────────────────────────────────────────────

    def save_term(self, term: int, voted_for: str | None) -> None:
        """
        Persist term and voted_for atomically (WAL write + fsync).

        This MUST be called before any response is sent to peers,
        per Raft §5.1: "Servers update their persistent state on
        stable storage before responding to RPCs."
        """
        self._wal.persist_term(term, voted_for)
        self._current_term = term
        self._voted_for = voted_for

    @property
    def current_term(self) -> int:
        return self._current_term

    @property
    def voted_for(self) -> str | None:
        return self._voted_for

    # ── Log operations ─────────────────────────────────────────────────────

    def append(self, entry: LogEntry) -> None:
        """Append a single entry to the durable log."""
        # WAL write first, then in-memory
        self._wal.append_entry(entry.index, entry.term, entry.command)
        self._log.append(entry)

    def append_many(self, entries: list[LogEntry]) -> None:
        """Append multiple entries. Each is individually fsynced."""
        for e in entries:
            self.append(e)

    def truncate_from(self, from_index: int) -> None:
        """
        Truncate log from from_index (inclusive).

        Used when a leader sends entries that conflict with local log.
        WAL is append-only — the truncation is recorded as a new
        term record that will cause recovery to trim the log.
        """
        if from_index <= self._snapshot_last_index:
            raise ValueError(
                f"Cannot truncate into snapshot: from_index={from_index} "
                f"snapshot_last_index={self._snapshot_last_index}"
            )
        # Find in-memory boundary
        mem_idx = self._mem_index(from_index)
        if mem_idx < 0:
            return  # Nothing to truncate
        removed = self._log[mem_idx:]
        self._log = self._log[:mem_idx]
        # Re-persist current term to signal truncation point to recovery
        self._wal.persist_term(self._current_term, self._voted_for)
        logger.debug("DurableLogStore: truncated %d entries from index %d", len(removed), from_index)

    def get(self, index: int) -> LogEntry | None:
        """Get entry at index. Returns None if index is before snapshot."""
        mem_idx = self._mem_index(index)
        if mem_idx < 0 or mem_idx >= len(self._log):
            return None
        return self._log[mem_idx]

    def get_range(self, from_index: int, to_index: int) -> list[LogEntry]:
        """Return entries [from_index, to_index] inclusive."""
        result = []
        for i in range(from_index, to_index + 1):
            e = self.get(i)
            if e is None:
                break
            result.append(e)
        return result

    def last_index(self) -> int:
        """Return the index of the last log entry, or snapshot_last_index if log is empty."""
        if self._log:
            return self._log[-1].index
        return self._snapshot_last_index

    def last_term(self) -> int:
        """Return the term of the last log entry."""
        if self._log:
            return self._log[-1].term
        return self._snapshot_last_term

    def __len__(self) -> int:
        return len(self._log)

    # ── Commit persistence ─────────────────────────────────────────────────

    def save_commit_index(self, commit_index: int) -> None:
        """Durably persist the commit index."""
        self._wal.persist_commit(commit_index)
        self._commit_index = commit_index
        # Mark entries as committed in memory
        for e in self._log:
            if e.index <= commit_index:
                e.committed = True

    @property
    def commit_index(self) -> int:
        return self._commit_index

    # ── Snapshot compaction ────────────────────────────────────────────────

    def compact_to_snapshot(
        self,
        last_included_index: int,
        last_included_term: int,
        snapshot_path: str,
    ) -> None:
        """
        Record a snapshot covering up to last_included_index and
        discard the corresponding log entries + WAL segments.
        """
        # Record snapshot pointer in WAL (before deleting segments)
        self._wal.record_snapshot_pointer(
            last_included_index, last_included_term, snapshot_path
        )

        # Truncate in-memory log
        self._log = [e for e in self._log if e.index > last_included_index]
        self._snapshot_last_index = last_included_index
        self._snapshot_last_term = last_included_term

        # Delete WAL segments that are fully covered by the snapshot
        deleted = self._wal.truncate_before(last_included_index)
        logger.info(
            "DurableLogStore: compacted to snapshot at index=%d term=%d "
            "(deleted %d WAL segments)",
            last_included_index, last_included_term, deleted,
        )

    # ── Internal ──────────────────────────────────────────────────────────

    def _mem_index(self, log_index: int) -> int:
        """Convert a global log index to an in-memory list index."""
        if not self._log:
            return -1
        offset = self._log[0].index
        mem_idx = log_index - offset
        if mem_idx < 0 or mem_idx >= len(self._log):
            return -1
        return mem_idx


class RecoveredState:
    """State returned by DurableLogStore.recover()."""

    def __init__(
        self,
        current_term: int,
        voted_for: str | None,
        commit_index: int,
        log: list[LogEntry],
        snapshot_last_index: int,
        snapshot_last_term: int,
    ) -> None:
        self.current_term = current_term
        self.voted_for = voted_for
        self.commit_index = commit_index
        self.log = log
        self.snapshot_last_index = snapshot_last_index
        self.snapshot_last_term = snapshot_last_term

    def __repr__(self) -> str:
        return (
            f"RecoveredState(term={self.current_term}, "
            f"voted_for={self.voted_for}, "
            f"commit_index={self.commit_index}, "
            f"entries={len(self.log)}, "
            f"snapshot_up_to={self.snapshot_last_index})"
        )
