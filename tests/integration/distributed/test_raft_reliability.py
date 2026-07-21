"""
Phase 13 Sprint 3 — Raft reliability validation.

These tests take the AEOS Raft core (app/distributed/consensus/raft.py) beyond
"has Raft" toward "Raft is production-grade" by driving the failure modes that
matter: leader death, quorum loss, network partition, and split-brain heal.

They run in one process (Raft is asyncio, transport-injected), but use a
CONTROLLABLE transport — `RoutableCluster` — whose links can be partitioned at
runtime and whose nodes can be crashed and restarted. That is what lets us
provoke real elections and partitions deterministically instead of mocking them.

What is proven here:
  - leader failover: killing the leader yields a NEW leader in a higher term;
  - quorum liveness: with a majority alive, the leader still commits;
  - quorum safety: with no majority, NO leader emerges and nothing commits;
  - partition: the minority side cannot commit; the majority side elects+commits;
  - split-brain heal: after the partition heals, the cluster converges to exactly
    one leader with no two leaders in the same term (INV-CONS-001).

Known limitations of this Raft core are documented, not hidden: it has no
pre-vote (a reconnecting partitioned candidate causes a term bump + re-election
on heal) and no next_index backoff loop (a node restarted with an empty log is
not guaranteed to re-sync a divergent suffix). Tests assert only what the
implementation actually guarantees; the evidence report (doc 030) records the gaps.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.distributed.consensus.raft import RaftNode, RaftRole
from app.distributed.validation.invariants import (
    check_raft_log_monotonicity,
    check_raft_single_leader,
)

# Fast timers so elections resolve in tens of ms (keeps the suite quick and
# shrinks the split-vote window). Ratios match the defaults (hb << el_min).
_FAST = dict(heartbeat_ms=20, election_min_ms=60, election_max_ms=120)


async def _wait(pred, timeout=5.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


class RoutableCluster:
    """In-process Raft cluster with a partitionable transport.

    rpc_send(src→dst) raises ConnectionError when either endpoint is crashed or
    the (undirected) link is partitioned — exactly what a real node observes when
    a peer is unreachable. Nodes keep running their tick loops during a partition,
    so elections happen for real.
    """

    def __init__(self, node_ids: list[str], **raft_kw):
        self.ids = list(node_ids)
        self.nodes: dict[str, RaftNode] = {}
        self._alive: set[str] = set(node_ids)
        self._blocked: set[frozenset] = set()
        self._raft_kw = raft_kw or dict(_FAST)
        for nid in node_ids:
            self.nodes[nid] = self._build(nid)

    def _build(self, nid: str) -> RaftNode:
        peers = [p for p in self.ids if p != nid]
        return RaftNode(nid, peers, self._make_rpc(nid), **self._raft_kw)

    def _make_rpc(self, src: str):
        async def rpc_send(dst: str, method: str, payload):
            # Reachability is evaluated at delivery time, so partitions/crashes
            # toggled mid-flight are honoured.
            if src not in self._alive or dst not in self._alive:
                raise ConnectionError(f"{src}->{dst}: node down")
            if frozenset({src, dst}) in self._blocked:
                raise ConnectionError(f"{src}->{dst}: partitioned")
            target = self.nodes.get(dst)
            if target is None:
                raise ConnectionError(f"{dst}: gone")
            if method == "request_vote":
                return await target.handle_vote_request(payload)
            if method == "append_entries":
                return await target.handle_append_entries(payload)
            raise ValueError(f"unknown method {method}")
        return rpc_send

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self):
        await asyncio.gather(*(n.start() for n in self.nodes.values()))

    async def stop(self):
        await asyncio.gather(*(n.stop() for n in self.nodes.values()),
                             return_exceptions=True)

    async def crash(self, nid: str):
        """Simulate a node crash: unreachable AND its tick loop halts."""
        self._alive.discard(nid)
        await self.nodes[nid].stop()

    async def restart(self, nid: str):
        """Restart a crashed node with a fresh (empty-log) in-memory state —
        mirrors MultiNodeCluster.restart_node (no WAL rewire)."""
        self.nodes[nid] = self._build(nid)
        self._alive.add(nid)
        await self.nodes[nid].start()

    # ── partitions ───────────────────────────────────────────────────────────
    def partition(self, group_a: list[str], group_b: list[str]):
        for a in group_a:
            for b in group_b:
                self._blocked.add(frozenset({a, b}))

    def heal(self):
        self._blocked.clear()

    # ── observation ──────────────────────────────────────────────────────────
    def alive_nodes(self) -> list[RaftNode]:
        return [self.nodes[i] for i in self.ids if i in self._alive]

    def leaders(self) -> list[RaftNode]:
        return [n for n in self.alive_nodes() if n.role == RaftRole.LEADER]

    def leader(self) -> RaftNode | None:
        ls = self.leaders()
        return max(ls, key=lambda n: n.term) if ls else None

    def stable_leader(self) -> RaftNode | None:
        """A single leader whose term no follower exceeds (i.e. not stale)."""
        ls = self.leaders()
        if len(ls) != 1:
            return None
        ldr = ls[0]
        top_term = max(n.term for n in self.alive_nodes())
        return ldr if ldr.term == top_term else None


# ── failover ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_leader_failover_on_leader_crash():
    """Killing the leader must yield a new leader in a higher term (RTO measured)."""
    c = RoutableCluster(["n1", "n2", "n3"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.leader() is not None), "no initial leader"
        old = c.leader()
        old_term = old.term

        t0 = time.monotonic()
        await c.crash(old._id)

        assert await _wait(lambda: c.stable_leader() is not None
                           and c.stable_leader()._id != old._id), \
            f"no failover; leaders={[n._id for n in c.leaders()]}"
        failover_s = time.monotonic() - t0

        new = c.stable_leader()
        assert new._id != old._id
        assert new.term > old_term
        # In-process failover should be well under a second with fast timers.
        assert failover_s < 3.0, f"failover took {failover_s:.3f}s"
        # No split-brain at steady state.
        assert await check_raft_single_leader(c.alive_nodes())() == []
    finally:
        await c.stop()


# ── quorum ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quorum_liveness_with_one_follower_down():
    """With a majority alive (2/3), the leader still commits proposals."""
    c = RoutableCluster(["n1", "n2", "n3"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.leader() is not None)
        leader = c.leader()
        follower = next(n for n in c.alive_nodes() if n._id != leader._id)
        other = next(n for n in c.alive_nodes()
                     if n._id not in (leader._id, follower._id))

        await c.crash(other._id)          # 2 of 3 remain → quorum intact
        assert leader.role == RaftRole.LEADER

        ok = await leader.propose({"op": "write", "k": "v"})
        assert ok is True
        # Committed on the majority: leader's commit_index advances to the entry.
        assert await _wait(lambda: leader._state.commit_index >= 0), \
            f"commit_index={leader._state.commit_index}"
        assert await check_raft_log_monotonicity(c.alive_nodes())() == []
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_quorum_safety_no_majority_no_progress():
    """With no majority (1/3 alive), NO new leader emerges and nothing commits.

    We crash the leader + one follower, leaving a lone FOLLOWER: it can never
    reach majority (2), so it spins elections without winning and cannot commit.
    (This Raft core has no leader-lease, so a surviving *leader* would keep its
    role while still being unable to commit — the safety property we assert is
    commit-safety, which holds in either framing.)"""
    c = RoutableCluster(["n1", "n2", "n3"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.leader() is not None)
        leader = c.leader()
        followers = [n._id for n in c.alive_nodes() if n._id != leader._id]
        # Crash the leader and one follower — a lone follower remains.
        await c.crash(leader._id)
        await c.crash(followers[0])
        lone = c.nodes[followers[1]]

        # Give it ample time to spin elections; it must NOT win (no quorum).
        await asyncio.sleep(1.0)
        assert lone.role != RaftRole.LEADER, f"lone node became leader (role={lone.role})"
        assert c.leader() is None
        # Safety: it cannot commit a proposal either.
        await lone.propose({"op": "x"})
        assert lone._state.commit_index == -1
    finally:
        await c.stop()


# ── partition + split-brain ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partition_minority_cannot_commit_majority_can():
    """5 nodes split 3|2 with the OLD leader in the minority: the minority cannot
    commit; the majority elects a new leader that can."""
    c = RoutableCluster(["n1", "n2", "n3", "n4", "n5"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.leader() is not None), "no initial leader"
        old = c.leader()
        minority = [old._id, next(i for i in c.ids if i != old._id)]
        majority = [i for i in c.ids if i not in minority]
        assert len(majority) == 3 and len(minority) == 2

        c.partition(minority, majority)

        # Majority side elects a leader and can commit.
        maj_nodes = [c.nodes[i] for i in majority]
        assert await _wait(lambda: any(n.role == RaftRole.LEADER for n in maj_nodes)), \
            "majority elected no leader"
        maj_leader = next(n for n in maj_nodes if n.role == RaftRole.LEADER)
        assert await maj_leader.propose({"op": "maj-write"}) is True
        assert await _wait(lambda: maj_leader._state.commit_index >= 0), \
            "majority leader could not commit with quorum"

        # Minority old leader: even if still 'leader' in its stale term, it has no
        # quorum, so a new proposal never commits.
        min_leader = c.nodes[minority[0]]
        before = min_leader._state.commit_index
        await min_leader.propose({"op": "min-write"})
        await asyncio.sleep(0.4)
        assert min_leader._state.commit_index == before, \
            "minority committed without quorum — SAFETY VIOLATION"
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_split_brain_heals_to_single_leader():
    """After a 3|2 partition heals, the cluster converges to exactly one leader
    with no two leaders in the same term (INV-CONS-001)."""
    c = RoutableCluster(["n1", "n2", "n3", "n4", "n5"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.leader() is not None)
        old = c.leader()
        minority = [old._id, next(i for i in c.ids if i != old._id)]
        majority = [i for i in c.ids if i not in minority]
        c.partition(minority, majority)

        maj_nodes = [c.nodes[i] for i in majority]
        assert await _wait(lambda: any(n.role == RaftRole.LEADER for n in maj_nodes))

        # Heal and let terms reconcile (higher term wins, disruptive candidate
        # forces a clean re-election since there is no pre-vote).
        c.heal()
        assert await _wait(lambda: c.stable_leader() is not None, timeout=6.0), \
            f"did not converge; leaders={[(n._id, n.term) for n in c.leaders()]}"

        # The core invariant: never two leaders in the same term.
        assert await check_raft_single_leader(c.alive_nodes())() == []
        assert len(c.leaders()) == 1
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_restarted_node_rejoins_under_current_leader():
    """Crash a follower and restart it fresh: the cluster keeps a single stable
    leader and the rejoined node accepts that leader (availability preserved).

    NOTE: this Raft core has no next_index backoff, so we assert re-integration
    of *leadership*, not full log convergence on the restarted node."""
    c = RoutableCluster(["n1", "n2", "n3"], **_FAST)
    await c.start()
    try:
        assert await _wait(lambda: c.stable_leader() is not None)
        leader = c.stable_leader()
        victim = next(n._id for n in c.alive_nodes() if n._id != leader._id)

        await c.crash(victim)
        assert await _wait(lambda: c.stable_leader() is not None), "lost leader on crash"

        await c.restart(victim)
        # The restarted node learns the current leader via heartbeat and follows.
        assert await _wait(lambda: c.nodes[victim].leader_id is not None), \
            "restarted node never learned a leader"
        assert await _wait(lambda: c.stable_leader() is not None)
        assert len(c.leaders()) == 1
        assert await check_raft_single_leader(c.alive_nodes())() == []
    finally:
        await c.stop()
