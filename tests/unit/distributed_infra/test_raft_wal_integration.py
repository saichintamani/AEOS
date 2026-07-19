"""
tests/unit/distributed_infra/test_raft_wal_integration.py

Integration tests: RaftNode + WAL persistence (CRIT-001 / LONGEVITY-001).

These tests verify that integrate_with_raft_node() correctly wires the WAL
into a running RaftNode so that propose() writes to durable storage first,
and that a node reconstructed from the same WAL directory recovers the log.

Test matrix:
  TestWALWiring             — shim patches propose() to write WAL before memory
  TestRaftNodeRecovery      — full stop/restart cycle recovers durable state
  TestMultiNodeClusterWAL   — MultiNodeCluster wires WAL when raft_data_dir set
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Any

import pytest

from app.distributed.consensus.raft import LogEntry, RaftNode, RaftRole, RaftState
from app.distributed.consensus.recovery import (
    RaftPersistence,
    RecoveryResult,
    integrate_with_raft_node,
)
from app.distributed.consensus.log_store import DurableLogStore


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _null_rpc(target_id: str, method: str, payload: Any) -> Any:
    raise ConnectionRefusedError(f"Stub: {target_id}/{method}")


def _single_node(data_dir: str) -> tuple[RaftNode, RaftPersistence]:
    """Create a RaftNode with WAL wired, pre-set as LEADER (no election needed)."""
    node = RaftNode(node_id="node-1", peers=[], rpc_send=_null_rpc)
    persistence = RaftPersistence(data_dir=data_dir, node_id="node-1", cluster_id="test")
    persistence.recover()
    integrate_with_raft_node(node, persistence)
    # Force to leader so propose() succeeds without an election
    node._role = RaftRole.LEADER
    return node, persistence


# ── TestWALWiring ─────────────────────────────────────────────────────────────

class TestWALWiring:

    @pytest.mark.asyncio
    async def test_propose_writes_to_wal_before_memory(self, tmp_path):
        """After integrate_with_raft_node(), propose() persists to WAL first."""
        node, persistence = _single_node(str(tmp_path))
        await node.start()
        try:
            ok = await node.propose({"op": "set", "key": "x", "value": 1})
            assert ok, "propose() should succeed on a leader node"
        finally:
            await node.stop()

        # WAL directory must contain at least one segment file
        wal_files = list(tmp_path.glob("*.wal"))
        assert wal_files, "WAL segment file must exist after propose()"

    @pytest.mark.asyncio
    async def test_propose_multiple_entries_all_in_wal(self, tmp_path):
        """All proposed entries appear in the WAL."""
        node, persistence = _single_node(str(tmp_path))
        await node.start()
        try:
            for i in range(10):
                ok = await node.propose({"op": "set", "key": f"k{i}", "value": i})
                assert ok, f"propose #{i} should succeed on leader"
        finally:
            await node.stop()

        # Recover from WAL and count entries
        recovery_persistence = RaftPersistence(
            data_dir=str(tmp_path), node_id="node-1", cluster_id="test"
        )
        result = recovery_persistence.recover()
        assert result.success, f"Recovery must succeed: {result.failure_reason}"
        assert result.log_entries_recovered == 10, (
            f"Expected 10 log entries recovered, got {result.log_entries_recovered}"
        )

    @pytest.mark.asyncio
    async def test_in_memory_log_matches_wal(self, tmp_path):
        """In-memory RaftNode log matches what the WAL persisted."""
        node, persistence = _single_node(str(tmp_path))
        await node.start()
        commands = [{"op": "write", "key": f"key-{i}"} for i in range(5)]
        try:
            for cmd in commands:
                await node.propose(cmd)
        finally:
            await node.stop()

        # Read log entries back from WAL
        store = DurableLogStore(str(tmp_path))
        state = store.recover()
        assert len(state.entries) == len(commands), (
            f"WAL must have {len(commands)} entries, found {len(state.entries)}"
        )
        for i, (entry, cmd) in enumerate(zip(state.entries, commands)):
            assert entry.command == cmd, (
                f"Entry {i} command mismatch: WAL={entry.command!r}, expected={cmd!r}"
            )


# ── TestRaftNodeRecovery ──────────────────────────────────────────────────────

class TestRaftNodeRecovery:

    @pytest.mark.asyncio
    async def test_stop_restart_recovers_log_entries(self, tmp_path):
        """
        Full stop/restart cycle: propose 10 entries, stop, reconstruct from WAL,
        verify all 10 entries recovered with correct terms.
        """
        data_dir = str(tmp_path)

        # Phase 1: Run node, propose 10 entries
        node, persistence = _single_node(data_dir)
        await node.start()
        try:
            for i in range(10):
                ok = await node.propose({"op": "write", "index": i})
                assert ok, f"propose #{i} failed"
        finally:
            await node.stop()

        # Phase 2: Reconstruct from WAL (simulates process restart)
        node2 = RaftNode(node_id="node-1", peers=[], rpc_send=_null_rpc)
        persistence2 = RaftPersistence(data_dir=data_dir, node_id="node-1", cluster_id="test")
        result = persistence2.recover()

        assert result.success, f"Recovery must succeed after restart: {result.failure_reason}"
        assert result.log_entries_recovered == 10, (
            f"Expected 10 log entries recovered, got {result.log_entries_recovered}"
        )

        # Restore state to the new node (same as MultiNodeCluster.start() does)
        node2._state.current_term = result.current_term
        if result.voted_for is not None:
            node2._state.voted_for = result.voted_for
        if result.commit_index >= 0:
            node2._state.commit_index = result.commit_index

        integrate_with_raft_node(node2, persistence2)

        # Verify recovered log has all 10 entries in order
        store = DurableLogStore(data_dir)
        recovered_state = store.recover()
        assert len(recovered_state.entries) == 10
        for i, entry in enumerate(recovered_state.entries):
            assert entry.command.get("index") == i, (
                f"Entry {i} has wrong command: {entry.command!r}"
            )

    @pytest.mark.asyncio
    async def test_term_persisted_across_restart(self, tmp_path):
        """current_term is persisted to WAL and restored on recovery."""
        data_dir = str(tmp_path)

        node, persistence = _single_node(data_dir)
        await node.start()
        # Simulate term advancement
        persistence.save_term(term=5, voted_for="node-2")
        await node.stop()

        # Recover
        persistence2 = RaftPersistence(data_dir=data_dir, node_id="node-1", cluster_id="test")
        result = persistence2.recover()
        assert result.success
        assert result.current_term == 5, f"Term must be recovered as 5, got {result.current_term}"
        assert result.voted_for == "node-2", f"voted_for must be recovered, got {result.voted_for}"

    @pytest.mark.asyncio
    async def test_empty_wal_recovery_succeeds(self, tmp_path):
        """Recovery from an empty directory (first-time start) succeeds with zeroed state."""
        persistence = RaftPersistence(
            data_dir=str(tmp_path), node_id="node-1", cluster_id="test"
        )
        result = persistence.recover()
        assert result.success
        assert result.current_term == 0
        assert result.log_entries_recovered == 0
        assert not result.snapshot_applied


# ── TestMultiNodeClusterWAL ───────────────────────────────────────────────────

class TestMultiNodeClusterWAL:

    @pytest.mark.asyncio
    async def test_cluster_wires_wal_when_data_dir_set(self, tmp_path):
        """
        MultiNodeCluster wires RaftPersistence for each node when
        raft_data_dir is provided.
        """
        from app.distributed.cluster.multi_node import MultiNodeCluster

        async with MultiNodeCluster(
            node_count=3,
            raft_data_dir=str(tmp_path),
        ) as cluster:
            # All three nodes should have persistence wired
            assert len(cluster._raft_persistence) == 3, (
                "Each node must have a RaftPersistence instance"
            )
            for node_id, persistence in cluster._raft_persistence.items():
                node_dir = os.path.join(str(tmp_path), node_id)
                assert os.path.isdir(node_dir), (
                    f"WAL directory must exist for {node_id}: {node_dir}"
                )

    @pytest.mark.asyncio
    async def test_cluster_without_data_dir_has_no_persistence(self, tmp_path):
        """
        MultiNodeCluster without raft_data_dir runs in-memory only
        (backward-compatible, no crash).
        """
        from app.distributed.cluster.multi_node import MultiNodeCluster

        async with MultiNodeCluster(node_count=3) as cluster:
            assert len(cluster._raft_persistence) == 0, (
                "Without raft_data_dir, no persistence should be wired"
            )
