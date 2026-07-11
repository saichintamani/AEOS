"""
Unit tests for Wave 9B.5.4 — RaftNode (in-process RPC, no network).

Strategy: wire 3-node cluster via a shared dict of RaftNodes and an
in-process rpc_send coroutine that routes directly by node_id.
All timers are bypassed by calling internal methods directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.distributed.consensus.raft import (
    AppendEntriesRequest,
    AppendEntriesResponse,
    LogEntry,
    RaftNode,
    RaftRole,
    VoteRequest,
    VoteResponse,
)


# ── In-process cluster fixture ─────────────────────���──────────────────────────

class InProcCluster:
    def __init__(self, node_ids: list[str]) -> None:
        self._nodes: dict[str, RaftNode] = {}
        for nid in node_ids:
            peers = [p for p in node_ids if p != nid]
            self._nodes[nid] = RaftNode(
                node_id=nid,
                peers=peers,
                rpc_send=self._make_rpc(nid),
            )

    def _make_rpc(self, from_id: str):
        cluster = self._nodes

        async def rpc_send(node_id: str, method: str, payload: Any) -> Any:
            target = cluster.get(node_id)
            if target is None:
                raise ConnectionError(f"node {node_id} not found")
            if method == "request_vote":
                return await target.handle_vote_request(payload)
            if method == "append_entries":
                return await target.handle_append_entries(payload)
            raise ValueError(f"unknown method: {method}")

        return rpc_send

    def node(self, nid: str) -> RaftNode:
        return self._nodes[nid]

    def all_nodes(self) -> list[RaftNode]:
        return list(self._nodes.values())

    async def start_all(self) -> None:
        for n in self._nodes.values():
            await n.start()

    async def stop_all(self) -> None:
        for n in self._nodes.values():
            await n.stop()


@pytest.fixture
def cluster():
    return InProcCluster(["n1", "n2", "n3"])


# ── Initial state ─────────────────────────────────────────────────────────────

class TestInitialState:

    def test_all_nodes_start_as_follower(self, cluster):
        for n in cluster.all_nodes():
            assert n.role == RaftRole.FOLLOWER

    def test_initial_term_is_zero(self, cluster):
        for n in cluster.all_nodes():
            assert n.term == 0

    def test_initial_log_empty(self, cluster):
        for n in cluster.all_nodes():
            assert n.log_size == 0

    def test_initial_leader_unknown(self, cluster):
        for n in cluster.all_nodes():
            assert n.leader_id is None


# ── Vote requests ─────────────────────────────────────────────────────────────

class TestVoteRequest:

    @pytest.mark.asyncio
    async def test_votes_granted_for_higher_term(self, cluster):
        n1 = cluster.node("n1")
        n2 = cluster.node("n2")

        req = VoteRequest(term=1, candidate_id="n1",
                          last_log_index=-1, last_log_term=0)
        resp = await n2.handle_vote_request(req)
        assert resp.vote_granted is True
        assert n2.term == 1

    @pytest.mark.asyncio
    async def test_vote_rejected_for_stale_term(self, cluster):
        n1 = cluster.node("n1")
        n2 = cluster.node("n2")

        # Advance n2 to term 5
        n2._state.current_term = 5

        req = VoteRequest(term=3, candidate_id="n1",
                          last_log_index=-1, last_log_term=0)
        resp = await n2.handle_vote_request(req)
        assert resp.vote_granted is False

    @pytest.mark.asyncio
    async def test_double_vote_in_same_term_rejected(self, cluster):
        n2 = cluster.node("n2")

        req1 = VoteRequest(term=2, candidate_id="n1",
                           last_log_index=-1, last_log_term=0)
        req2 = VoteRequest(term=2, candidate_id="n3",
                           last_log_index=-1, last_log_term=0)

        r1 = await n2.handle_vote_request(req1)
        r2 = await n2.handle_vote_request(req2)
        assert r1.vote_granted is True
        assert r2.vote_granted is False   # INV-RAFT-001

    @pytest.mark.asyncio
    async def test_vote_granted_same_candidate_idempotent(self, cluster):
        n2 = cluster.node("n2")
        req = VoteRequest(term=2, candidate_id="n1",
                          last_log_index=-1, last_log_term=0)
        r1 = await n2.handle_vote_request(req)
        r2 = await n2.handle_vote_request(req)
        assert r1.vote_granted is True
        assert r2.vote_granted is True   # same candidate, same term


# ── Leader election ───────────────────────────────────────────────────────────

class TestLeaderElection:

    @pytest.mark.asyncio
    async def test_election_makes_node_leader(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()
        assert n1.role == RaftRole.LEADER

    @pytest.mark.asyncio
    async def test_leader_knows_own_id(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()
        assert n1.leader_id == "n1"

    @pytest.mark.asyncio
    async def test_followers_learn_leader_from_heartbeat(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()

        # Leader sends heartbeats
        await n1._send_heartbeats()
        await asyncio.sleep(0.05)

        # Other nodes should know the leader
        assert cluster.node("n2").leader_id == "n1"
        assert cluster.node("n3").leader_id == "n1"

    @pytest.mark.asyncio
    async def test_term_increases_on_election(self, cluster):
        n1 = cluster.node("n1")
        assert n1.term == 0
        await n1._start_election()
        assert n1.term == 1


# ── Log replication ───────────────────────────────────────────────────────────

class TestLogReplication:

    @pytest.mark.asyncio
    async def test_propose_only_succeeds_on_leader(self, cluster):
        n1 = cluster.node("n1")
        n2 = cluster.node("n2")

        # Before election, n1 is follower
        ok = await n1.propose({"op": "test"})
        assert ok is False

        await n1._start_election()
        ok = await n1.propose({"op": "test"})
        assert ok is True

    @pytest.mark.asyncio
    async def test_log_entry_replicated_to_followers(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()

        await n1.propose({"op": "set", "key": "x", "value": 1})

        # After replication n2 and n3 should have the entry
        assert cluster.node("n2").log_size >= 1
        assert cluster.node("n3").log_size >= 1

    @pytest.mark.asyncio
    async def test_log_entry_committed_after_quorum(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()
        await n1.propose({"op": "set", "key": "y", "value": 2})

        # Quorum = 2/3 nodes — n1 + one follower is enough
        assert n1._state.commit_index >= 0

    @pytest.mark.asyncio
    async def test_apply_callback_called_on_commit(self, cluster):
        n1 = cluster.node("n1")
        applied: list[LogEntry] = []
        n1.on_apply(lambda e: applied.append(e))

        await n1._start_election()
        await n1.propose({"op": "test"})

        assert len(applied) >= 1
        assert applied[0].command["op"] == "test"


# ── AppendEntries ─────────────────────────────────────────────────────────────

class TestAppendEntries:

    @pytest.mark.asyncio
    async def test_append_entries_rejected_for_stale_term(self, cluster):
        n2 = cluster.node("n2")
        n2._state.current_term = 5

        req = AppendEntriesRequest(
            term=3, leader_id="n1", prev_log_index=-1, prev_log_term=0,
            entries=[], leader_commit=-1,
        )
        resp = await n2.handle_append_entries(req)
        assert resp.success is False

    @pytest.mark.asyncio
    async def test_append_entries_accepted_as_follower(self, cluster):
        n2 = cluster.node("n2")

        req = AppendEntriesRequest(
            term=1, leader_id="n1", prev_log_index=-1, prev_log_term=0,
            entries=[], leader_commit=-1,
        )
        resp = await n2.handle_append_entries(req)
        assert resp.success is True
        assert n2.role == RaftRole.FOLLOWER
        assert n2.leader_id == "n1"

    @pytest.mark.asyncio
    async def test_append_entries_with_entries(self, cluster):
        n2 = cluster.node("n2")

        entry = LogEntry(term=1, index=0, command={"op": "set"})
        req = AppendEntriesRequest(
            term=1, leader_id="n1", prev_log_index=-1, prev_log_term=0,
            entries=[entry], leader_commit=0,
        )
        resp = await n2.handle_append_entries(req)
        assert resp.success is True
        assert n2.log_size == 1

    @pytest.mark.asyncio
    async def test_conflicting_log_entries_truncated(self, cluster):
        n2 = cluster.node("n2")

        # Append an entry at term 1
        e1 = LogEntry(term=1, index=0, command={"op": "old"})
        req1 = AppendEntriesRequest(
            term=1, leader_id="n1", prev_log_index=-1, prev_log_term=0,
            entries=[e1], leader_commit=-1,
        )
        await n2.handle_append_entries(req1)
        assert n2.log_size == 1

        # New leader at term 2 sends conflicting entry
        e2 = LogEntry(term=2, index=0, command={"op": "new"})
        req2 = AppendEntriesRequest(
            term=2, leader_id="n3", prev_log_index=-1, prev_log_term=0,
            entries=[e2], leader_commit=-1,
        )
        await n2.handle_append_entries(req2)
        assert n2.log_size == 1
        assert n2._state.log[0].command["op"] == "new"


# ── Term update ───────────────────────────────────────────────────────────────

class TestTermUpdate:

    @pytest.mark.asyncio
    async def test_higher_term_resets_to_follower(self, cluster):
        n1 = cluster.node("n1")
        await n1._start_election()
        assert n1.role == RaftRole.LEADER

        # Simulate receiving a message with higher term
        n1._update_term(10)
        assert n1.role == RaftRole.FOLLOWER
        assert n1.term == 10

    @pytest.mark.asyncio
    async def test_higher_term_clears_voted_for(self, cluster):
        n1 = cluster.node("n1")
        n1._state.voted_for = "n2"
        n1._update_term(5)
        assert n1._state.voted_for is None


# ── Invariants ────────────────────────────────────────────────────────────────

class TestRaftInvariants:

    @pytest.mark.asyncio
    async def test_inv_raft_001_at_most_one_vote_per_term(self, cluster):
        """INV-RAFT-001: A server grants at most one vote per term."""
        n2 = cluster.node("n2")

        votes_granted = 0
        for candidate in ["n1", "n3"]:
            req = VoteRequest(term=4, candidate_id=candidate,
                              last_log_index=-1, last_log_term=0)
            resp = await n2.handle_vote_request(req)
            if resp.vote_granted:
                votes_granted += 1

        assert votes_granted <= 1

    @pytest.mark.asyncio
    async def test_inv_raft_003_commit_only_after_quorum(self, cluster):
        """INV-RAFT-003: entries committed only when replicated on majority."""
        n1 = cluster.node("n1")
        await n1._start_election()

        # Before propose, commit_index is -1
        assert n1._state.commit_index == -1

        # Propose triggers replication; with 3 nodes, 2 agree → quorum
        await n1.propose({"op": "x"})
        assert n1._state.commit_index >= 0

    @pytest.mark.asyncio
    async def test_inv_raft_004_state_machine_applies_in_order(self, cluster):
        """INV-RAFT-004: state machine applies entries in log order."""
        n1 = cluster.node("n1")
        order: list[int] = []
        n1.on_apply(lambda e: order.append(e.index))

        await n1._start_election()
        await n1.propose({"op": "a"})
        await n1.propose({"op": "b"})
        await n1.propose({"op": "c"})

        assert order == sorted(order), "entries not applied in log order"
