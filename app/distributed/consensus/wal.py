"""
app/distributed/consensus/wal.py

Write-Ahead Log (WAL) — append-only, fsync-durable storage for Raft log entries.

Design principles:
  1. Crash-safe: every write is fsynced before acknowledgement
  2. Append-only: segments are never modified after write (corruption-safe)
  3. Checksummed: every record carries a CRC32 so corruption is detected,
     not silently propagated
  4. Segmented: WAL rolls to a new segment at SEGMENT_MAX_BYTES to bound
     replay time and enable efficient compaction
  5. Replayable: starting from any valid offset, the WAL can reconstruct
     full log state deterministically

Record format (binary, little-endian):
  [8 bytes  ] magic    = 0xAE05_CAFE_DEAD_BEEF
  [4 bytes  ] crc32    = CRC32(length_bytes + data)
  [4 bytes  ] length   = len(data) in bytes
  [N bytes  ] data     = JSON-encoded WAL record

Segment naming:
  wal-{first_index:020d}.seg

Recovery reads all segments in order, replays valid records,
stops at the first corrupted or truncated record.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_MAGIC = 0xAE05CAFEDEADBEEF
_HEADER_FMT = "<QII"          # magic (8), crc32 (4), length (4)
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 16

SEGMENT_MAX_BYTES = 64 * 1024 * 1024   # 64 MiB per segment


class WALCorruption(Exception):
    """Raised when a WAL record fails integrity checks."""


class WALTruncated(Exception):
    """Raised when a WAL record is partially written (truncated at crash)."""


# ── Record types ───────────────────────────────────────────────────────────

@dataclass
class WALRecord:
    """
    A single WAL record.

    record_type: one of "entry", "term", "vote", "commit", "snapshot_ptr"
    payload:     the data for this record type
    """
    record_type: str
    payload: dict[str, Any]

    def to_bytes(self) -> bytes:
        return json.dumps(
            {"type": self.record_type, "payload": self.payload},
            separators=(",", ":"),
        ).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "WALRecord":
        obj = json.loads(data)
        return cls(record_type=obj["type"], payload=obj["payload"])


# ── Segment ────────────────────────────────────────────────────────────────

class WALSegment:
    """
    One WAL segment file.

    Opened in append mode. Every write is followed by an fsync.
    Reads scan from the beginning for recovery.
    """

    def __init__(self, path: Path, first_index: int) -> None:
        self.path = path
        self.first_index = first_index
        self._size = path.stat().st_size if path.exists() else 0
        self._fd: int | None = None

    def open_write(self) -> None:
        """Open segment for appending (creates if not exists)."""
        self._fd = os.open(
            str(self.path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def append(self, record: WALRecord) -> int:
        """
        Append a record and fsync. Returns the byte offset of this record.
        Thread-safe only if called from a single thread (asyncio task).
        """
        if self._fd is None:
            raise RuntimeError("WALSegment not open for writing")

        data = record.to_bytes()
        length = len(data)
        crc = zlib.crc32(struct.pack("<I", length) + data) & 0xFFFFFFFF
        header = struct.pack(_HEADER_FMT, _MAGIC, crc, length)
        payload = header + data

        offset = self._size
        os.write(self._fd, payload)
        os.fsync(self._fd)
        self._size += len(payload)
        return offset

    @property
    def size(self) -> int:
        return self._size

    @property
    def full(self) -> bool:
        return self._size >= SEGMENT_MAX_BYTES

    def iter_records(self) -> Iterator[WALRecord]:
        """
        Iterate over all valid records in this segment.
        Stops at corruption or truncation — does NOT raise.
        """
        with open(self.path, "rb") as f:
            while True:
                header_bytes = f.read(_HEADER_SIZE)
                if not header_bytes:
                    return  # Clean EOF
                if len(header_bytes) < _HEADER_SIZE:
                    logger.warning("WAL segment %s: truncated header at EOF", self.path)
                    return

                try:
                    magic, crc, length = struct.unpack(_HEADER_FMT, header_bytes)
                except struct.error as exc:
                    logger.error("WAL segment %s: header unpack error: %s", self.path, exc)
                    return

                if magic != _MAGIC:
                    logger.error(
                        "WAL segment %s: bad magic 0x%016X (expected 0x%016X)",
                        self.path, magic, _MAGIC,
                    )
                    return

                data = f.read(length)
                if len(data) < length:
                    logger.warning(
                        "WAL segment %s: truncated data (got %d, want %d)",
                        self.path, len(data), length,
                    )
                    return

                actual_crc = zlib.crc32(struct.pack("<I", length) + data) & 0xFFFFFFFF
                if actual_crc != crc:
                    logger.error(
                        "WAL segment %s: CRC mismatch (stored=%08X actual=%08X) — "
                        "stopping replay at this record",
                        self.path, crc, actual_crc,
                    )
                    return

                try:
                    yield WALRecord.from_bytes(data)
                except (json.JSONDecodeError, KeyError) as exc:
                    logger.error("WAL segment %s: record decode error: %s", self.path, exc)
                    return


# ── WAL ───────────────────────────────────────────────────────────────────

class WriteAheadLog:
    """
    Multi-segment, fsync-durable, checksummed Write-Ahead Log.

    Usage::

        wal = WriteAheadLog(data_dir="/var/lib/aeos/raft")
        wal.open()

        # Write
        wal.append_entry(index=0, term=1, command={"op": "noop"})
        wal.persist_term(term=1, voted_for="node-2")
        wal.persist_commit(commit_index=0)

        # On restart — recover
        wal2 = WriteAheadLog(data_dir="/var/lib/aeos/raft")
        state = wal2.recover()
        # state contains: entries, current_term, voted_for, commit_index

    Thread safety:
        Not thread-safe. Designed for use from a single asyncio task.
        Wrap in asyncio.Lock if called from multiple coroutines.
    """

    def __init__(self, data_dir: str) -> None:
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._active: WALSegment | None = None
        self._segments: list[WALSegment] = []

    def open(self) -> None:
        """
        Open the WAL for writing.
        Discovers existing segments; creates initial segment if none exist.
        """
        self._segments = self._discover_segments()
        if self._segments and not self._segments[-1].full:
            active = self._segments[-1]
        else:
            first_idx = 0
            if self._segments:
                # New segment starts after last known index
                last_seg = self._segments[-1]
                last_idx = self._last_index_in_segment(last_seg)
                first_idx = last_idx + 1
            active = self._create_segment(first_idx)
            self._segments.append(active)
        active.open_write()
        self._active = active
        logger.info(
            "WAL opened: dir=%s segments=%d active=%s",
            self._dir, len(self._segments), active.path.name,
        )

    def close(self) -> None:
        """Flush and close the active segment."""
        if self._active:
            self._active.close()
            self._active = None

    # ── Write operations ──────────────────────────────────────────────────

    def append_entry(self, index: int, term: int, command: dict[str, Any]) -> None:
        """Append a log entry. Fsynced before returning."""
        self._write(WALRecord(
            record_type="entry",
            payload={"index": index, "term": term, "command": command},
        ))

    def persist_term(self, term: int, voted_for: str | None) -> None:
        """Persist current_term and voted_for atomically."""
        self._write(WALRecord(
            record_type="term",
            payload={"term": term, "voted_for": voted_for},
        ))

    def persist_commit(self, commit_index: int) -> None:
        """Persist the durable commit index."""
        self._write(WALRecord(
            record_type="commit",
            payload={"commit_index": commit_index},
        ))

    def record_snapshot_pointer(self, last_included_index: int, last_included_term: int,
                                 snapshot_path: str) -> None:
        """Record that a snapshot exists, covering entries up to last_included_index."""
        self._write(WALRecord(
            record_type="snapshot_ptr",
            payload={
                "last_included_index": last_included_index,
                "last_included_term": last_included_term,
                "snapshot_path": snapshot_path,
            },
        ))

    # ── Recovery ──────────────────────────────────────────────────────────

    def recover(self) -> dict[str, Any]:
        """
        Replay all WAL segments and return the recovered Raft state.

        Returns::

            {
                "current_term": int,
                "voted_for": str | None,
                "commit_index": int,
                "entries": list[dict],         # {index, term, command}
                "snapshot_ptr": dict | None,   # {last_included_index, ...}
            }

        Stops replay at the first corrupted record, preserving all
        valid state up to that point.
        """
        self._segments = self._discover_segments()

        state: dict[str, Any] = {
            "current_term": 0,
            "voted_for": None,
            "commit_index": -1,
            "entries": [],
            "snapshot_ptr": None,
        }

        total_records = 0
        for seg in self._segments:
            for record in seg.iter_records():
                total_records += 1
                rt = record.record_type
                p = record.payload

                if rt == "term":
                    state["current_term"] = max(state["current_term"], p["term"])
                    state["voted_for"] = p["voted_for"]

                elif rt == "entry":
                    idx = p["index"]
                    # Truncate any conflicting entries (term mismatch)
                    entries: list = state["entries"]
                    if idx < len(entries):
                        entries = entries[:idx]
                        state["entries"] = entries
                    entries.append({"index": idx, "term": p["term"], "command": p["command"]})

                elif rt == "commit":
                    state["commit_index"] = max(state["commit_index"], p["commit_index"])

                elif rt == "snapshot_ptr":
                    state["snapshot_ptr"] = p
                    # Entries before the snapshot are compacted away
                    snap_idx = p["last_included_index"]
                    state["entries"] = [
                        e for e in state["entries"] if e["index"] > snap_idx
                    ]

        logger.info(
            "WAL recovery complete: %d segments, %d records, "
            "term=%d commit_index=%d entries=%d",
            len(self._segments), total_records,
            state["current_term"], state["commit_index"], len(state["entries"]),
        )
        return state

    # ── Compaction ────────────────────────────────────────────────────────

    def truncate_before(self, last_included_index: int) -> int:
        """
        Delete WAL segments that are fully covered by a snapshot.
        Returns number of segments deleted.
        """
        to_delete = []
        keep = []
        for seg in self._segments:
            last_in_seg = self._last_index_in_segment(seg)
            if last_in_seg >= 0 and last_in_seg <= last_included_index:
                to_delete.append(seg)
            else:
                keep.append(seg)

        for seg in to_delete:
            try:
                seg.path.unlink()
                logger.info("WAL compaction: deleted segment %s", seg.path.name)
            except OSError as exc:
                logger.error("WAL compaction: failed to delete %s: %s", seg.path.name, exc)

        self._segments = keep
        return len(to_delete)

    # ── Internal ──────────────────────────────────────────────────────────

    def _write(self, record: WALRecord) -> None:
        if self._active is None:
            raise RuntimeError("WAL not open — call open() first")
        if self._active.full:
            self._active.close()
            new_first = self._next_first_index()
            new_seg = self._create_segment(new_first)
            new_seg.open_write()
            self._segments.append(new_seg)
            self._active = new_seg
            logger.info("WAL rolled to new segment: %s", new_seg.path.name)
        self._active.append(record)

    def _discover_segments(self) -> list[WALSegment]:
        """Find all .seg files, sorted by first_index."""
        segs = []
        for path in sorted(self._dir.glob("wal-*.seg")):
            try:
                first_idx = int(path.stem.split("-")[1])
                segs.append(WALSegment(path, first_idx))
            except (ValueError, IndexError):
                logger.warning("WAL: ignoring unexpected file: %s", path.name)
        return segs

    def _create_segment(self, first_index: int) -> WALSegment:
        path = self._dir / f"wal-{first_index:020d}.seg"
        return WALSegment(path, first_index)

    def _next_first_index(self) -> int:
        if not self._segments:
            return 0
        last = self._last_index_in_segment(self._segments[-1])
        return last + 1 if last >= 0 else 0

    def _last_index_in_segment(self, seg: WALSegment) -> int:
        """Scan segment to find its last log index. Returns -1 if no entries."""
        last = -1
        for record in seg.iter_records():
            if record.record_type == "entry":
                last = max(last, record.payload.get("index", -1))
        return last
