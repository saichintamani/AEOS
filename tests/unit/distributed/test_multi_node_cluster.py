"""
Tests for Phase 9B.6 Priority 2 — MultiNodeCluster.

Covers:
  - 3-node, 5-node, 10-node cluster startup
  - Raft leader election in each topology
  - ClusterSnapshot (healthy_count, has_leader, is_healthy)
  - Node crash and membership state change
  - Node restart and re-election
  - Capability advertisement via federator
  - propose_via_leader
  - NodeConfig and NodeHandle accessors
  - ClusterSnapshot properties
"""

from __future__ import annotations

import asyncio

import pytest

from app.distributed.cluster.multi_node import (
    ClusterSnapshot,
    MultiNodeCluster,
    NodeConfig,
    NodeHandle,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _cluster(n: int, **kwargs) -> MultiNodeCluster:
    """Create and start a cluster of n nodes."""
    c = MultiNodeCluster(node_count=n, **kwargs)
    await c.start()
    return c


# ── Cluster startup ───────────────────────────────────────────────────────────

class TestClusterStartup:

    @pytest.mark.asyncio
    async def test_three_node_starts(self):
        cluster = await _cluster(3)
        try:
            assert len(cluster.node_ids()) == 3
            assert len(cluster.alive_nodes()) == 3
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_five_node_starts(self):
        cluster = await _cluster(5)
        try:
            assert len(cluster.node_ids()) == 5
            assert len(cluster.alive_nodes()) == 5
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_ten_node_starts(self):
        cluster = await _cluster(10)
        try:
            assert len(cluster.node_ids()) == 10
            assert len(cluster.alive_nodes()) == 10
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_context_manager_start_stop(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            assert len(cluster.node_ids()) == 3
        # After exit, _running should be False
        assert cluster._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self):
        cluster = MultiNodeCluster(node_count=3)
        await cluster.start()
        try:
            count_before = len(cluster._raft_tasks)
            await cluster.start()  # second start — should be no-op
            assert len(cluster._raft_tasks) == count_before
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_single_node_cluster(self):
        cluster = await _cluster(1)
        try:
            assert len(cluster.node_ids()) == 1
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_custom_node_configs(self):
        configs = [
            NodeConfig(node_id="alpha", host="10.0.0.1", port=7001, role_hint="coordinator"),
            NodeConfig(node_id="beta",  host="10.0.0.2", port=7002, role_hint="worker"),
            NodeConfig(node_id="gamma", host="10.0.0.3", port=7003, role_hint="worker"),
        ]
        cluster = MultiNodeCluster(node_count=3, node_configs=configs)
        await cluster.start()
        try:
            assert "alpha" in cluster.node_ids()
            assert "beta" in cluster.node_ids()
            assert "gamma" in cluster.node_ids()
        finally:
            await cluster.stop()


# ── Raft Leader election ──────────────────────────────────────────────────────

class TestLeaderElection:

    @pytest.mark.asyncio
    async def test_leader_elected_three_nodes(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            assert leader is not None
            assert leader in cluster.node_ids()

    @pytest.mark.asyncio
    async def test_leader_elected_five_nodes(self):
        async with MultiNodeCluster(node_count=5) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            assert leader is not None
            assert leader in cluster.node_ids()

    @pytest.mark.asyncio
    async def test_only_one_leader_at_a_time(self):
        from app.distributed.consensus.raft import RaftRole
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            leaders = [
                h.node_id
                for h in cluster._nodes.values()
                if h.raft.role == RaftRole.LEADER
            ]
            assert len(leaders) == 1

    @pytest.mark.asyncio
    async def test_wait_for_leader_returns_string(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            assert isinstance(leader, str)
            assert len(leader) > 0

    @pytest.mark.asyncio
    async def test_current_leader_matches_wait_result(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            current = cluster.current_leader()
            assert current == leader


# ── ClusterSnapshot ───────────────────────────────────────────────────────────

class TestClusterSnapshot:

    @pytest.mark.asyncio
    async def test_snapshot_node_count(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.node_count == 3

    @pytest.mark.asyncio
    async def test_snapshot_has_leader(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.has_leader is True
            assert snap.leader_id is not None

    @pytest.mark.asyncio
    async def test_snapshot_healthy_count(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.healthy_count == 3

    @pytest.mark.asyncio
    async def test_snapshot_is_healthy(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.is_healthy is True

    @pytest.mark.asyncio
    async def test_snapshot_raft_roles_present(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert len(snap.raft_roles) == 3
            assert any(r == "leader" for r in snap.raft_roles.values())

    @pytest.mark.asyncio
    async def test_snapshot_capability_count(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            snap = await cluster.snapshot()
            assert snap.capability_count == 3   # one advertisement per node

    @pytest.mark.asyncio
    async def test_snapshot_leader_term_positive(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.leader_term >= 1

    @pytest.mark.asyncio
    async def test_five_node_snapshot_is_healthy(self):
        async with MultiNodeCluster(node_count=5) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            snap = await cluster.snapshot()
            assert snap.is_healthy is True
            assert snap.healthy_count == 5


# ── Node crash and restart ────────────────────────────────────────────────────

class TestNodeCrashAndRestart:

    @pytest.mark.asyncio
    async def test_crash_follower_node(self):
        from app.distributed.consensus.raft import RaftRole

        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            # Find a non-leader node
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            assert follower not in cluster.alive_nodes()
            assert cluster.get_handle(follower).alive is False

    @pytest.mark.asyncio
    async def test_crash_updates_member_state(self):
        from app.distributed.contracts.cluster import ClusterMemberState

        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            snap = await cluster.snapshot()
            # The crashed node's state should be FAILED or absent
            state = snap.member_states.get(follower)
            assert state in ("FAILED", None)

    @pytest.mark.asyncio
    async def test_crash_non_leader_cluster_still_healthy(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            # Leader should still be elected
            current = cluster.current_leader()
            assert current is not None

    @pytest.mark.asyncio
    async def test_crash_unknown_node_raises(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            with pytest.raises(KeyError):
                await cluster.crash_node("node-nonexistent")

    @pytest.mark.asyncio
    async def test_restart_crashed_node_rejoins(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            assert follower not in cluster.alive_nodes()

            await cluster.restart_node(follower)
            assert follower in cluster.alive_nodes()

    @pytest.mark.asyncio
    async def test_restart_node_appears_in_membership(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            await cluster.restart_node(follower)

            members = await cluster._store.all()
            node_ids = {m.node_id for m in members}
            assert follower in node_ids


# ── Capability federation ─────────────────────────────────────────────────────

class TestCapabilityFederation:

    @pytest.mark.asyncio
    async def test_all_nodes_advertise_capabilities(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            profiles = await cluster._federator.profiles()
            assert len(profiles) == 3

    @pytest.mark.asyncio
    async def test_node_config_capabilities_reflected(self):
        configs = [
            NodeConfig(
                node_id="llm-node",
                capabilities=["llm", "planning"],
                model_hint="claude-3-opus",
            ),
            NodeConfig(
                node_id="search-node",
                capabilities=["llm", "search", "rag"],
                model_hint="claude-3-haiku",
            ),
            NodeConfig(
                node_id="vision-node",
                capabilities=["llm", "vision"],
                model_hint="claude-3-sonnet",
            ),
        ]
        cluster = MultiNodeCluster(node_count=3, node_configs=configs)
        await cluster.start()
        try:
            profiles = await cluster._federator.profiles()
            worker_ids = {p.worker_id for p in profiles}
            assert "llm-node" in worker_ids
            assert "search-node" in worker_ids
            assert "vision-node" in worker_ids
        finally:
            await cluster.stop()

    @pytest.mark.asyncio
    async def test_five_node_all_advertise(self):
        async with MultiNodeCluster(node_count=5) as cluster:
            profiles = await cluster._federator.profiles()
            assert len(profiles) == 5


# ── Log replication ───────────────────────────────────────────────────────────

class TestLogReplication:

    @pytest.mark.asyncio
    async def test_propose_via_leader_succeeds(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            result = await cluster.propose(leader, {"op": "set", "key": "x", "val": 1})
            assert result is True

    @pytest.mark.asyncio
    async def test_propose_via_leader_helper(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            result = await cluster.propose_via_leader({"op": "noop"}, timeout=5.0)
            assert result is True

    @pytest.mark.asyncio
    async def test_propose_on_dead_node_raises(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            follower = next(
                nid for nid in cluster.node_ids() if nid != leader
            )
            await cluster.crash_node(follower)
            with pytest.raises(KeyError):
                await cluster.propose(follower, {"op": "noop"})

    @pytest.mark.asyncio
    async def test_log_grows_after_proposals(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
            handle = cluster.get_handle(leader)
            initial_log_size = handle.raft.log_size

            await cluster.propose(leader, {"op": "set", "k": "a"})
            await cluster.propose(leader, {"op": "set", "k": "b"})

            assert handle.raft.log_size >= initial_log_size + 2


# ── NodeHandle ────────────────────────────────────────────────────────────────

class TestNodeHandle:

    @pytest.mark.asyncio
    async def test_get_handle_returns_handle(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            nid = cluster.node_ids()[0]
            handle = cluster.get_handle(nid)
            assert isinstance(handle, NodeHandle)

    @pytest.mark.asyncio
    async def test_get_handle_unknown_raises(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            with pytest.raises(KeyError):
                cluster.get_handle("no-such-node")

    @pytest.mark.asyncio
    async def test_handle_node_id(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            nid = cluster.node_ids()[0]
            handle = cluster.get_handle(nid)
            assert handle.node_id == nid

    @pytest.mark.asyncio
    async def test_handle_raft_role(self):
        from app.distributed.consensus.raft import RaftRole

        async with MultiNodeCluster(node_count=3) as cluster:
            await cluster.wait_for_leader(timeout=5.0)
            for nid in cluster.node_ids():
                handle = cluster.get_handle(nid)
                assert handle.raft_role in RaftRole

    @pytest.mark.asyncio
    async def test_handle_alive_default_true(self):
        async with MultiNodeCluster(node_count=3) as cluster:
            nid = cluster.node_ids()[0]
            handle = cluster.get_handle(nid)
            assert handle.alive is True


# ── ClusterSnapshot dataclass ─────────────────────────────────────────────────

class TestClusterSnapshotDataclass:

    def test_snapshot_no_leader(self):
        snap = ClusterSnapshot(
            taken_at=0.0,
            node_count=3,
            leader_id=None,
            leader_term=0,
            raft_roles={},
            member_states={"n1": "RUNNING", "n2": "RUNNING", "n3": "RUNNING"},
            capability_count=3,
        )
        assert snap.has_leader is False
        assert snap.is_healthy is False

    def test_snapshot_partial_failure(self):
        snap = ClusterSnapshot(
            taken_at=0.0,
            node_count=3,
            leader_id="n1",
            leader_term=1,
            raft_roles={"n1": "leader", "n2": "follower", "n3": "follower"},
            member_states={"n1": "RUNNING", "n2": "RUNNING", "n3": "FAILED"},
            capability_count=2,
        )
        assert snap.healthy_count == 2
        assert snap.has_leader is True
        assert snap.is_healthy is False   # healthy_count != node_count

    def test_snapshot_fully_healthy(self):
        snap = ClusterSnapshot(
            taken_at=0.0,
            node_count=3,
            leader_id="n1",
            leader_term=2,
            raft_roles={"n1": "leader", "n2": "follower", "n3": "follower"},
            member_states={"n1": "RUNNING", "n2": "RUNNING", "n3": "RUNNING"},
            capability_count=3,
        )
        assert snap.healthy_count == 3
        assert snap.is_healthy is True


# ── invalid config ────────────────────────────────────────────────────────────

class TestInvalidConfig:

    def test_zero_node_count_raises(self):
        with pytest.raises(ValueError):
            MultiNodeCluster(node_count=0)

    def test_negative_node_count_raises(self):
        with pytest.raises(ValueError):
            MultiNodeCluster(node_count=-1)
