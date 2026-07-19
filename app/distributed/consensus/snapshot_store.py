"""
app/distributed/consensus/snapshot_store.py

Raft Snapshot Store — durable state machine snapshots for log compaction.

A snapshot captures the full state machine state at a given log index,
allowing WAL segments before that index to be deleted.

File layout::

    <data_dir>/
        snapshots/
            snap-{last_included_index:020d}-{last_included_term:010d}.snap
            snap-{last_included_index:020d}-{last_included_term:010d}.snap.meta

.snap file:      JSON-encoded state machine state (gzip compressed)
.snap.meta file: JSON metadata with checksums and provenance

Design:
  - Atomic writes: snapshot written to .tmp, fsync'd, then renamed
  - Checksummed: SHA-256 of compressed data stored in .meta
  - Versioned: format_version field for forward compatibility
  - Retention: keeps last N snapshots (default 3) for safety

Integration:
  DurableLogStore calls snapshot_store.save() after applying entries,
  then calls compact_to_snapshot() to trim the WAL.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_FORMAT_VERSION = 1
_KEEP_SNAPSHOTS = 3   # Retain last N snapshots


@dataclass
class SnapshotMeta:
    """Metadata accompanying a snapshot file."""
    format_version: int
    last_included_index: int
    last_included_term: int
    created_at: float
    node_id: str
    cluster_id: str
    sha256: str
    compressed_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "last_included_index": self.last_included_index,
            "last_included_term": self.last_included_term,
            "created_at": self.created_at,
            "node_id": self.node_id,
            "cluster_id": self.cluster_id,
            "sha256": self.sha256,
            "compressed_size": self.compressed_size,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SnapshotMeta":
        return cls(
            format_version=d["format_version"],
            last_included_index=d["last_included_index"],
            last_included_term=d["last_included_term"],
            created_at=d["created_at"],
            node_id=d["node_id"],
            cluster_id=d["cluster_id"],
            sha256=d["sha256"],
            compressed_size=d["compressed_size"],
        )


@dataclass
class Snapshot:
    meta: SnapshotMeta
    state: dict[str, Any]       # Full state machine state

    @property
    def last_included_index(self) -> int:
        return self.meta.last_included_index

    @property
    def last_included_term(self) -> int:
        return self.meta.last_included_term


class SnapshotCorruption(Exception):
    """Raised when a snapshot fails integrity checks."""


class SnapshotStore:
    """
    Atomic, checksummed, crash-safe snapshot storage.

    Usage::

        store = SnapshotStore(data_dir="/var/lib/aeos/raft/node-1")
        store.open()

        # Save a snapshot
        snap = store.save(
            state={"task_states": {...}, "raft_term": 5},
            last_included_index=1000,
            last_included_term=3,
            node_id="node-1",
            cluster_id="prod-cluster",
        )

        # Load latest on recovery
        snap = store.load_latest()
        if snap:
            apply_state(snap.state)
    """

    def __init__(self, data_dir: str, keep: int = _KEEP_SNAPSHOTS) -> None:
        self._snap_dir = Path(data_dir) / "snapshots"
        self._keep = keep

    def open(self) -> None:
        """Create snapshot directory if it doesn't exist."""
        self._snap_dir.mkdir(parents=True, exist_ok=True)
        logger.info("SnapshotStore opened: %s", self._snap_dir)

    def save(
        self,
        state: dict[str, Any],
        last_included_index: int,
        last_included_term: int,
        node_id: str = "",
        cluster_id: str = "",
    ) -> Snapshot:
        """
        Atomically save a snapshot.

        Write path:
          1. Serialize state → JSON → gzip compress
          2. Compute SHA-256 of compressed bytes
          3. Write compressed data to .snap.tmp
          4. fsync the temp file
          5. Write .meta.tmp
          6. fsync the meta file
          7. Rename .snap.tmp → .snap (atomic on POSIX)
          8. Rename .meta.tmp → .meta
          9. Prune old snapshots
        """
        basename = (
            f"snap-{last_included_index:020d}-{last_included_term:010d}"
        )
        snap_path = self._snap_dir / f"{basename}.snap"
        meta_path = self._snap_dir / f"{basename}.snap.meta"
        tmp_snap = self._snap_dir / f"{basename}.snap.tmp"
        tmp_meta = self._snap_dir / f"{basename}.snap.meta.tmp"

        # Serialize and compress
        raw = json.dumps(state, separators=(",", ":")).encode()
        compressed = gzip.compress(raw, compresslevel=6)
        sha256 = hashlib.sha256(compressed).hexdigest()

        meta = SnapshotMeta(
            format_version=_FORMAT_VERSION,
            last_included_index=last_included_index,
            last_included_term=last_included_term,
            created_at=time.time(),
            node_id=node_id,
            cluster_id=cluster_id,
            sha256=sha256,
            compressed_size=len(compressed),
        )

        # Write snapshot (atomic)
        tmp_snap.write_bytes(compressed)
        with open(tmp_snap, "ab") as f:
            os.fsync(f.fileno())

        # Write meta (atomic)
        tmp_meta.write_text(
            json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
        )
        with open(tmp_meta, "a") as f:
            os.fsync(f.fileno())

        # Atomic rename (POSIX guarantees)
        tmp_snap.rename(snap_path)
        tmp_meta.rename(meta_path)

        logger.info(
            "Snapshot saved: index=%d term=%d size=%d bytes sha256=%s...",
            last_included_index, last_included_term, len(compressed), sha256[:16],
        )

        self._prune_old_snapshots()

        return Snapshot(meta=meta, state=state)

    def load_latest(self) -> Snapshot | None:
        """
        Load and verify the most recent valid snapshot.

        Returns None if no snapshots exist.
        Raises SnapshotCorruption if the latest snapshot is corrupt
        and no valid older snapshot exists.
        """
        candidates = self._list_snapshots()
        for snap_path, meta_path in reversed(candidates):
            try:
                snap = self._load_one(snap_path, meta_path)
                logger.info(
                    "Snapshot loaded: index=%d term=%d",
                    snap.last_included_index, snap.last_included_term,
                )
                return snap
            except SnapshotCorruption as exc:
                logger.error("Snapshot corrupt (%s): %s — trying older", snap_path.name, exc)
                continue

        return None

    def load_at_index(self, index: int) -> Snapshot | None:
        """Load the snapshot covering the given index, if it exists."""
        for snap_path, meta_path in self._list_snapshots():
            meta = self._load_meta(meta_path)
            if meta and meta.last_included_index == index:
                try:
                    return self._load_one(snap_path, meta_path)
                except SnapshotCorruption:
                    return None
        return None

    def list_available(self) -> list[SnapshotMeta]:
        """Return metadata for all available snapshots (newest first)."""
        result = []
        for _, meta_path in reversed(self._list_snapshots()):
            meta = self._load_meta(meta_path)
            if meta:
                result.append(meta)
        return result

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_one(self, snap_path: Path, meta_path: Path) -> Snapshot:
        """Load and verify a single snapshot. Raises SnapshotCorruption on failure."""
        meta = self._load_meta(meta_path)
        if meta is None:
            raise SnapshotCorruption(f"Cannot read meta file: {meta_path}")

        if not snap_path.exists():
            raise SnapshotCorruption(f"Snapshot file missing: {snap_path}")

        compressed = snap_path.read_bytes()

        # Integrity check
        actual_sha256 = hashlib.sha256(compressed).hexdigest()
        if actual_sha256 != meta.sha256:
            raise SnapshotCorruption(
                f"SHA-256 mismatch: stored={meta.sha256[:16]} "
                f"actual={actual_sha256[:16]}"
            )

        if len(compressed) != meta.compressed_size:
            raise SnapshotCorruption(
                f"Size mismatch: stored={meta.compressed_size} "
                f"actual={len(compressed)}"
            )

        # Decompress and parse
        try:
            raw = gzip.decompress(compressed)
            state = json.loads(raw)
        except (gzip.BadGzipFile, json.JSONDecodeError, OSError) as exc:
            raise SnapshotCorruption(f"Decompression/parse failed: {exc}") from exc

        return Snapshot(meta=meta, state=state)

    def _load_meta(self, meta_path: Path) -> SnapshotMeta | None:
        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
            return SnapshotMeta.from_dict(raw)
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.error("Cannot read snapshot meta %s: %s", meta_path, exc)
            return None

    def _list_snapshots(self) -> list[tuple[Path, Path]]:
        """Return (snap_path, meta_path) pairs sorted by index (oldest first)."""
        pairs = []
        for snap_path in sorted(self._snap_dir.glob("snap-*.snap")):
            if snap_path.suffix != ".snap" or ".meta" in snap_path.name:
                continue
            meta_path = snap_path.with_suffix(".snap.meta")
            pairs.append((snap_path, meta_path))
        return pairs

    def _prune_old_snapshots(self) -> None:
        """Delete snapshots beyond the retention limit."""
        all_snaps = self._list_snapshots()
        to_delete = all_snaps[: max(0, len(all_snaps) - self._keep)]
        for snap_path, meta_path in to_delete:
            for path in (snap_path, meta_path):
                try:
                    path.unlink(missing_ok=True)
                    logger.debug("Pruned old snapshot: %s", path.name)
                except OSError as exc:
                    logger.warning("Failed to prune %s: %s", path.name, exc)
