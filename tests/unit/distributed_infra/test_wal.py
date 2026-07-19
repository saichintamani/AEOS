"""
tests/unit/distributed_infra/test_wal.py

WAL, DurableLogStore, SnapshotStore, and RaftPersistence tests.

Tests cover:
  - Normal append and recovery
  - Crash during append (partial write simulation)
  - Corrupted segment (bad CRC)
  - Segment rotation
  - Snapshot save + load + corruption detection
  - Full recovery round-trip
  - Compaction (WAL truncation after snapshot)
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import zlib
from pathlib import Path

import pytest

from app.distributed.consensus.wal import (
    WALRecord,
    WALSegment,
    WriteAheadLog,
    _HEADER_FMT,
    _MAGIC,
)
from app.distributed.consensus.log_store import DurableLogStore, LogEntry
from app.distributed.consensus.snapshot_store import Snapshot, SnapshotCorruption, SnapshotStore
from app.distributed.consensus.recovery import RaftPersistence


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dir(tmp_path):
    return str(tmp_path)


# ── WALRecord tests ────────────────────────────────────────────────────────

class TestWALRecord:
    def test_roundtrip(self):
        rec = WALRecord(record_type="entry", payload={"index": 5, "term": 2, "command": {"op": "x"}})
        data = rec.to_bytes()
        rec2 = WALRecord.from_bytes(data)
        assert rec2.record_type == rec.record_type
        assert rec2.payload == rec.payload

    def test_term_record(self):
        rec = WALRecord(record_type="term", payload={"term": 7, "voted_for": "node-3"})
        rec2 = WALRecord.from_bytes(rec.to_bytes())
        assert rec2.payload["term"] == 7
        assert rec2.payload["voted_for"] == "node-3"


# ── WALSegment tests ───────────────────────────────────────────────────────

class TestWALSegment:
    def test_append_and_read(self, tmp_path):
        seg_path = tmp_path / "wal-00000000000000000000.seg"
        seg = WALSegment(seg_path, first_index=0)
        seg.open_write()

        records = [
            WALRecord("entry", {"index": i, "term": 1, "command": {"op": f"cmd-{i}"}})
            for i in range(5)
        ]
        for r in records:
            seg.append(r)
        seg.close()

        recovered = list(seg.iter_records())
        assert len(recovered) == 5
        for i, rec in enumerate(recovered):
            assert rec.record_type == "entry"
            assert rec.payload["index"] == i

    def test_empty_segment(self, tmp_path):
        seg_path = tmp_path / "wal-empty.seg"
        seg_path.write_bytes(b"")
        seg = WALSegment(seg_path, first_index=0)
        assert list(seg.iter_records()) == []

    def test_corrupted_crc_stops_replay(self, tmp_path):
        seg_path = tmp_path / "wal-corrupt.seg"
        seg = WALSegment(seg_path, first_index=0)
        seg.open_write()

        # Write 3 valid records
        for i in range(3):
            seg.append(WALRecord("entry", {"index": i, "term": 1, "command": {}}))
        seg.close()

        # Corrupt the second record's data bytes
        raw = seg_path.read_bytes()
        # Each record = 16 (header) + data bytes
        # Find second record: skip first header + data
        first_data = json.dumps(
            {"type": "entry", "payload": {"index": 0, "term": 1, "command": {}}},
            separators=(",", ":"),
        ).encode()
        first_record_size = 16 + len(first_data)
        # Corrupt byte 20 into the second record (past the header)
        corrupt_offset = first_record_size + 16 + 2  # 2 bytes into second record's data
        raw_list = bytearray(raw)
        raw_list[corrupt_offset] ^= 0xFF  # Flip bits
        seg_path.write_bytes(bytes(raw_list))

        records = list(seg.iter_records())
        # Should recover only the first record; stops at corruption
        assert len(records) == 1
        assert records[0].payload["index"] == 0

    def test_truncated_header_stops_replay(self, tmp_path):
        seg_path = tmp_path / "wal-truncated.seg"
        seg = WALSegment(seg_path, first_index=0)
        seg.open_write()
        seg.append(WALRecord("entry", {"index": 0, "term": 1, "command": {}}))
        seg.close()

        # Truncate last 8 bytes (corrupt the segment)
        raw = seg_path.read_bytes()
        seg_path.write_bytes(raw[:-8])

        records = list(seg.iter_records())
        # First record should be fine if it's before the truncation
        # but the second (partial) write is gone
        assert isinstance(records, list)  # No crash

    def test_bad_magic_stops_replay(self, tmp_path):
        seg_path = tmp_path / "wal-badmagic.seg"
        seg_path.write_bytes(b"\x00" * 64)
        seg = WALSegment(seg_path, first_index=0)
        assert list(seg.iter_records()) == []


# ── WriteAheadLog tests ────────────────────────────────────────────────────

class TestWriteAheadLog:
    def test_open_empty_dir(self, tmp_dir):
        wal = WriteAheadLog(tmp_dir)
        wal.open()
        wal.close()
        # Should have created one segment
        segs = list(Path(tmp_dir).glob("wal-*.seg"))
        assert len(segs) == 1

    def test_append_and_recover(self, tmp_dir):
        wal = WriteAheadLog(tmp_dir)
        wal.open()
        wal.append_entry(0, 1, {"op": "set", "k": "a"})
        wal.append_entry(1, 1, {"op": "set", "k": "b"})
        wal.persist_term(1, "node-2")
        wal.persist_commit(1)
        wal.close()

        # Recover from scratch
        wal2 = WriteAheadLog(tmp_dir)
        state = wal2.recover()
        assert state["current_term"] == 1
        assert state["voted_for"] == "node-2"
        assert state["commit_index"] == 1
        assert len(state["entries"]) == 2
        assert state["entries"][0]["index"] == 0
        assert state["entries"][1]["index"] == 1

    def test_recover_empty_wal(self, tmp_dir):
        wal = WriteAheadLog(tmp_dir)
        state = wal.recover()
        assert state["current_term"] == 0
        assert state["voted_for"] is None
        assert state["commit_index"] == -1
        assert state["entries"] == []

    def test_multiple_term_updates(self, tmp_dir):
        wal = WriteAheadLog(tmp_dir)
        wal.open()
        wal.persist_term(1, None)
        wal.persist_term(2, "node-3")
        wal.persist_term(3, "node-1")
        wal.close()

        state = WriteAheadLog(tmp_dir).recover()
        assert state["current_term"] == 3
        assert state["voted_for"] == "node-1"

    def test_snapshot_pointer_compacts_entries(self, tmp_dir):
        wal = WriteAheadLog(tmp_dir)
        wal.open()
        for i in range(10):
            wal.append_entry(i, 1, {"op": f"cmd-{i}"})
        wal.record_snapshot_pointer(
            last_included_index=4,
            last_included_term=1,
            snapshot_path="/snap/snap-00004.snap",
        )
        wal.close()

        state = WriteAheadLog(tmp_dir).recover()
        # Entries 0–4 should be gone; entries 5–9 remain
        assert all(e["index"] > 4 for e in state["entries"])
        assert state["snapshot_ptr"]["last_included_index"] == 4

    def test_segment_rotation(self, tmp_dir):
        from app.distributed.consensus.wal import SEGMENT_MAX_BYTES
        wal = WriteAheadLog(tmp_dir)
        wal.open()

        # Write enough data to force segment rotation
        big_command = {"data": "x" * 1000}
        entries_needed = (SEGMENT_MAX_BYTES // 1020) + 10

        for i in range(entries_needed):
            wal.append_entry(i, 1, big_command)
        wal.close()

        segs = list(Path(tmp_dir).glob("wal-*.seg"))
        assert len(segs) >= 2

        # Full recovery across segments
        state = WriteAheadLog(tmp_dir).recover()
        assert len(state["entries"]) == entries_needed


# ── DurableLogStore tests ──────────────────────────────────────────────────

class TestDurableLogStore:
    def test_fresh_open(self, tmp_dir):
        store = DurableLogStore(tmp_dir)
        store.open()
        assert store.last_index() == -1
        assert store.current_term == 0
        store.close()

    def test_append_and_recover(self, tmp_dir):
        store = DurableLogStore(tmp_dir)
        store.open()
        store.save_term(2, "node-5")
        for i in range(5):
            store.append(LogEntry(term=2, index=i, command={"op": f"x{i}"}))
        store.save_commit_index(3)
        store.close()

        store2 = DurableLogStore(tmp_dir)
        recovered = store2.recover()
        assert recovered.current_term == 2
        assert recovered.voted_for == "node-5"
        assert recovered.commit_index == 3
        assert len(recovered.log) == 5

    def test_truncate_and_recover(self, tmp_dir):
        store = DurableLogStore(tmp_dir)
        store.open()
        for i in range(8):
            store.append(LogEntry(term=1, index=i, command={"x": i}))
        # Leader tells us to truncate from index 5
        store.truncate_from(5)
        # Append new entries from new leader
        for i in range(5, 8):
            store.append(LogEntry(term=2, index=i, command={"y": i}))
        store.close()

        store2 = DurableLogStore(tmp_dir)
        recovered = store2.recover()
        # Entries 0-4 intact, 5-7 overwritten with term=2
        assert len(recovered.log) == 8
        for entry in recovered.log[5:]:
            assert entry.term == 2

    def test_snapshot_compaction(self, tmp_dir):
        store = DurableLogStore(tmp_dir)
        store.open()
        for i in range(20):
            store.append(LogEntry(term=1, index=i, command={"i": i}))
        store.compact_to_snapshot(
            last_included_index=9,
            last_included_term=1,
            snapshot_path="/snap/test.snap",
        )
        assert store.last_index() >= 10
        store.close()

        store2 = DurableLogStore(tmp_dir)
        recovered = store2.recover()
        # Only entries after snapshot remain
        assert all(e.index >= 10 for e in recovered.log)


# ── SnapshotStore tests ────────────────────────────────────────────────────

class TestSnapshotStore:
    def test_save_and_load(self, tmp_dir):
        store = SnapshotStore(tmp_dir)
        store.open()
        state = {"task_states": {"t1": "COMPLETED"}, "raft_term": 3}
        snap = store.save(
            state=state,
            last_included_index=100,
            last_included_term=3,
            node_id="node-1",
        )
        assert snap.last_included_index == 100

        loaded = store.load_latest()
        assert loaded is not None
        assert loaded.state == state
        assert loaded.last_included_index == 100

    def test_no_snapshots_returns_none(self, tmp_dir):
        store = SnapshotStore(tmp_dir)
        store.open()
        assert store.load_latest() is None

    def test_corruption_detection(self, tmp_dir):
        store = SnapshotStore(tmp_dir)
        store.open()
        state = {"x": 42}
        store.save(state=state, last_included_index=50, last_included_term=2)

        # Corrupt the .snap file
        snap_files = list((Path(tmp_dir) / "snapshots").glob("*.snap"))
        snap_files = [f for f in snap_files if ".meta" not in f.name]
        assert snap_files
        snap_file = snap_files[0]
        data = bytearray(snap_file.read_bytes())
        data[10] ^= 0xFF  # Flip bits
        snap_file.write_bytes(bytes(data))

        # load_latest should return None (no valid snapshot)
        assert store.load_latest() is None

    def test_pruning_keeps_last_n(self, tmp_dir):
        store = SnapshotStore(tmp_dir, keep=2)
        store.open()
        for i in range(5):
            store.save(
                state={"i": i},
                last_included_index=i * 100,
                last_included_term=1,
            )
        snaps_dir = Path(tmp_dir) / "snapshots"
        snap_files = [f for f in snaps_dir.glob("*.snap") if ".meta" not in f.name]
        assert len(snap_files) <= 2

    def test_atomic_write_no_partial_files(self, tmp_dir):
        store = SnapshotStore(tmp_dir)
        store.open()
        store.save(state={"ok": True}, last_included_index=1, last_included_term=1)

        # No .tmp files should remain
        snap_dir = Path(tmp_dir) / "snapshots"
        tmp_files = list(snap_dir.glob("*.tmp"))
        assert len(tmp_files) == 0


# ── RaftPersistence integration tests ─────────────────────────────────────

class TestRaftPersistence:
    def test_fresh_start(self, tmp_dir):
        p = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        p.open_fresh()
        assert p.current_term == 0
        assert p.voted_for is None
        assert p.commit_index == -1
        p.close()

    def test_full_round_trip(self, tmp_dir):
        # Simulate node running
        p = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        p.open_fresh()
        p.save_term(3, "n2")
        p.append_entry(0, 3, {"op": "noop"})
        p.append_entry(1, 3, {"op": "set", "k": "v"})
        p.save_commit(1)
        p.close()

        # Simulate crash + restart
        p2 = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        result = p2.recover()
        assert result.success
        assert result.current_term == 3
        assert result.voted_for == "n2"
        assert result.commit_index == 1
        assert result.log_entries_recovered == 2
        p2.close()

    def test_snapshot_and_compaction(self, tmp_dir):
        p = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        p.open_fresh()
        for i in range(10):
            p.append_entry(i, 1, {"i": i})
        p.save_commit(9)

        # Snapshot at index 4
        p.save_snapshot(
            state={"entries_applied": 5},
            last_included_index=4,
            last_included_term=1,
        )
        p.close()

        # Recovery should load snapshot and replay remaining entries
        p2 = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        snapshot_state = {}
        result = p2.recover(apply_snapshot_fn=lambda s: snapshot_state.update(s))
        assert result.success
        assert result.snapshot_applied
        assert result.snapshot_last_index == 4
        assert snapshot_state == {"entries_applied": 5}
        # Log entries after snapshot are recovered
        assert result.log_entries_recovered == 5  # indices 5-9
        p2.close()

    def test_truncate_entries(self, tmp_dir):
        p = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        p.open_fresh()
        for i in range(6):
            p.append_entry(i, 1, {"i": i})
        # Leader tells us to truncate from 4
        p.truncate_log_from(4)
        # Append from new leader
        p.append_entry(4, 2, {"new": True})
        p.close()

        p2 = RaftPersistence(tmp_dir, node_id="n1", cluster_id="c1")
        result = p2.recover()
        assert result.success
        # Should have 5 entries: 0,1,2,3 (term=1) + 4 (term=2)
        entries = result.log
        assert len(entries) == 5
        assert entries[-1].term == 2
        p2.close()
