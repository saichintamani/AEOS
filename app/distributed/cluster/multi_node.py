"""
Phase 9B.6 Priority 2 — Multi-Node Cluster Deployment

Provides `MultiNodeCluster`: an in-process N-node cluster that wires together:
  - Raft consensus (election + replication)
  - ClusterMemberManager (PROTO-001/002/003)
  - CapabilityFederator (per-node capability advertisement)
  - Health probing and cluster status snapshots

Supports 3-node, 5-node, and 10-node topologies without external infrastructure.

Usage::

    async with MultiNodeCluster(node_count=3) as cluster:
        leader = await cluster.wait_for_leader(timeout=5.0)
        snapshot = await cluster.snapshot()
        assert snapshot.healthy_count == 3

Design notes:
  - Each node gets a RaftNode wired via in-process RPC routing
  - Each node has a ClusterMemberManager backed by a shared InMemoryMembershipStore
  - CapabilityFederator is shared (coordinator-side) — nodes advertise on join
  - NodeRecord tracks per-node live handles
  - ClusterSnapshot provides a point-in-time cluster health view
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.distributed.capability.federation import (
    CapabilityAdvertisement,
    CapabilityCategory,
    CapabilityFederator,
    LLMCapability,
)
from app.distributed.cluster.manager import ClusterMemberManager
from app.distributed.cluster.membership import InMemoryMembershipStore
from app.distributed.consensus.raft import RaftNode, RaftRole
from app.distributed.consensus.recovery import RaftPersistence, integrate_with_raft_node
from app.distributed.contracts.cluster import (
    ClusterMemberState,
    MemberRecord,
    NodeIdentity,
)

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class NodeConfig:
    """Configuration for a single cluster node."""
    node_id: str
    host: str = "127.0.0.1"
    port: int = 0                      # 0 = ephemeral (in-process, no real socket)
    role_hint: str = "worker"          # worker | coordinator | storage
    capabilities: list[str] = field(default_factory=list)
    model_hint: str = ""               # LLM model name hint, if any


@dataclass
class NodeHandle:
    """Live handles for a running cluster node."""
    config: NodeConfig
    raft: RaftNode
    member: MemberRecord | None = None
    started_at: float = field(default_factory=time.monotonic)
    _alive: bool = True

    @property
    def node_id(self) -> str:
        return self.config.node_id

    @property
    def alive(self) -> bool:
        return self._alive

    @property
    def raft_role(self) -> RaftRole:
        return self.raft.role

    @property
    def raft_term(self) -> int:
        return self.raft.term


@dataclass
class ClusterSnapshot:
    """Point-in-time cluster health snapshot."""
    taken_at: float
    node_count: int
    leader_id: str | None
    leader_term: int
    raft_roles: dict[str, str]          # node_id → role name
    member_states: dict[str, str]       # node_id → member state
    capability_count: int               # total advertised capabilities

    @property
    def healthy_count(self) -> int:
        return sum(1 for s in self.member_states.values() if s == "RUNNING")

    @property
    def has_leader(self) -> bool:
        return self.leader_id is not None

    @property
    def is_healthy(self) -> bool:
        return self.has_leader and self.healthy_count == self.node_count


# ── MultiNodeCluster ──────────────────────────────────────────────────────────

class MultiNodeCluster:
    """
    In-process N-node AEOS cluster.

    Wire-up:
      1. N RaftNodes with in-process RPC routing (no network sockets)
      2. Shared InMemoryMembershipStore + one ClusterMemberManager per node
      3. Shared CapabilityFederator — nodes advertise on start
      4. Background tasks: Raft tick loops + membership heartbeats

    All nodes are started concurrently. The cluster is ready once a Raft
    leader is elected and all nodes are in RUNNING member state.

    Usage as context manager::

        async with MultiNodeCluster(3) as cluster:
            leader = await cluster.wait_for_leader(timeout=5.0)
    """

    def __init__(
        self,
        node_count: int = 3,
        *,
        node_configs: list[NodeConfig] | None = None,
        heartbeat_interval_s: float = 5.0,
        election_timeout_factor: float = 1.0,
        raft_data_dir: str | None = None,
    ) -> None:
        if node_count < 1:
            raise ValueError("node_count must be >= 1")

        self._node_count = node_count
        self._heartbeat_interval = heartbeat_interval_s

        # Build default configs if not provided
        if node_configs is None:
            node_configs = [
                NodeConfig(
                    node_id=f"node-{i+1}",
                    host="127.0.0.1",
                    port=10000 + i,
                    role_hint="coordinator" if i == 0 else "worker",
                    capabilities=["llm", "planning"] if i == 0 else ["llm"],
                    model_hint="claude-3-haiku" if i < 3 else "claude-3-sonnet",
                )
                for i in range(node_count)
            ]
        self._configs = node_configs

        # Shared infrastructure
        self._store = InMemoryMembershipStore()
        self._federator = CapabilityFederator()

        # Per-node handles, keyed by node_id
        self._nodes: dict[str, NodeHandle] = {}

        # Raft RPC routing table: node_id → RaftNode
        self._raft_nodes: dict[str, RaftNode] = {}

        # When set, each RaftNode gets a durable WAL under <raft_data_dir>/<node_id>/
        self._raft_data_dir = raft_data_dir

        self._running = False
        self._raft_tasks: list[asyncio.Task] = []
        self._raft_persistence: dict[str, RaftPersistence] = {}

    # ── Context manager ───────────────────────────────────────────────────────

    async def __aenter__(self) -> "MultiNodeCluster":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start all nodes concurrently."""
        if self._running:
            return
        self._running = True

        # Build RPC router closure over _raft_nodes dict.
        # Raft calls: rpc_send(peer_id, "request_vote", VoteRequest)
        #             rpc_send(peer_id, "append_entries", AppendEntriesRequest)
        async def _rpc(target_id: str, method: str, payload: Any) -> Any:
            node = self._raft_nodes.get(target_id)
            if node is None:
                raise ConnectionRefusedError(f"Node {target_id} not found")
            if method == "request_vote":
                return await node.handle_vote_request(payload)
            elif method == "append_entries":
                return await node.handle_append_entries(payload)
            raise ValueError(f"Unknown method: {method}")

        # Create all RaftNodes first (so router can reference them)
        for cfg in self._configs:
            peers = [c.node_id for c in self._configs if c.node_id != cfg.node_id]
            raft = RaftNode(
                node_id=cfg.node_id,
                peers=peers,
                rpc_send=_rpc,
            )
            # Wire WAL persistence when a data directory is configured.
            # Without this call, RaftNode operates on in-memory state only and
            # loses all consensus state on restart (CRIT-001 / LONGEVITY-001).
            if self._raft_data_dir:
                import os
                node_dir = os.path.join(self._raft_data_dir, cfg.node_id)
                persistence = RaftPersistence(
                    data_dir=node_dir,
                    node_id=cfg.node_id,
                    cluster_id="aeos-cluster",
                )
                result = persistence.recover()
                if result.success:
                    # Restore durable term and log to the in-memory RaftState
                    raft._state.current_term = result.current_term
                    raft._state.voted_for = result.voted_for
                    if result.commit_index >= 0:
                        raft._state.commit_index = result.commit_index
                    logger.info(
                        "Raft recovery: node=%s term=%d log_entries=%d",
                        cfg.node_id, result.current_term, result.log_entries_recovered,
                    )
                integrate_with_raft_node(raft, persistence)
                self._raft_persistence[cfg.node_id] = persistence
            self._raft_nodes[cfg.node_id] = raft

        # Start Raft nodes and register members concurrently
        await asyncio.gather(*[self._start_node(cfg) for cfg in self._configs])

        logger.info(
            "MultiNodeCluster started: %d nodes, ids=%s",
            self._node_count,
            [c.node_id for c in self._configs],
        )

    async def _start_node(self, cfg: NodeConfig) -> None:
        """Start a single node: Raft + membership + capabilities."""
        raft = self._raft_nodes[cfg.node_id]

        # Start Raft (sets _running=True, spawns tick loop internally)
        await raft.start()
        # Track the task for cancellation on cluster.stop()
        if raft._tasks:
            self._raft_tasks.extend(raft._tasks)

        # Register membership
        identity = NodeIdentity(
            node_id=cfg.node_id,
            host=cfg.host,
            port=cfg.port,
            metadata={"role": cfg.role_hint},
        )
        manager = ClusterMemberManager(
            self._store,
            heartbeat_interval_seconds=self._heartbeat_interval,
            failure_check_interval_seconds=self._heartbeat_interval * 2,
        )
        try:
            member = await manager.join(identity)
            member.state = ClusterMemberState.RUNNING
            await self._store.update(member)
        except Exception:
            member = MemberRecord(identity=identity, state=ClusterMemberState.RUNNING)

        # Advertise capabilities
        adv = self._build_advertisement(cfg)
        await self._federator.advertise(adv)

        handle = NodeHandle(config=cfg, raft=raft, member=member)
        self._nodes[cfg.node_id] = handle
        logger.debug("Node started: %s (%s)", cfg.node_id, cfg.role_hint)

    def _build_advertisement(self, cfg: NodeConfig) -> CapabilityAdvertisement:
        caps = set(cfg.capabilities)
        llm = None
        if "llm" in caps:
            llm = LLMCapability(
                models=[cfg.model_hint or "claude-3-haiku"],
                max_context_tokens=200_000,
                supports_function_calling=True,
            )
        return CapabilityAdvertisement(
            worker_id=cfg.node_id,
            cpu_cores=4,
            memory_gb=16.0,
            current_load=0.1,
            llm=llm,
            has_vision="vision" in caps,
            has_search="search" in caps,
            has_rag="rag" in caps,
            skills=frozenset(caps),
        )

    async def stop(self) -> None:
        """Stop all nodes gracefully."""
        if not self._running:
            return
        self._running = False

        # Stop all Raft nodes via their public stop() method
        for handle in self._nodes.values():
            try:
                await handle.raft.stop()
            except Exception:
                pass
        self._raft_tasks.clear()

        logger.info("MultiNodeCluster stopped")

    # ── Leader election ───────────────────────────────────────────────────────

    async def wait_for_leader(self, timeout: float = 5.0) -> str:
        """
        Wait until a Raft leader is elected.

        Returns the leader's node_id.
        Raises TimeoutError if no leader elected within `timeout` seconds.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            leader = self.current_leader()
            if leader is not None:
                return leader
            await asyncio.sleep(0.05)
        raise TimeoutError(
            f"No leader elected within {timeout}s for "
            f"{self._node_count}-node cluster"
        )

    def current_leader(self) -> str | None:
        """Return the current Raft leader node_id, or None."""
        for handle in self._nodes.values():
            if handle.raft.role == RaftRole.LEADER:
                return handle.node_id
        return None

    # ── Node manipulation ─────────────────────────────────────────────────────

    async def crash_node(self, node_id: str) -> None:
        """
        Simulate a node crash: cancel its Raft task and mark FAILED.
        """
        handle = self._nodes.get(node_id)
        if handle is None:
            raise KeyError(f"Unknown node: {node_id}")
        handle._alive = False

        # Stop Raft for this node and remove from RPC routing
        try:
            await handle.raft.stop()
        except Exception:
            pass
        self._raft_nodes.pop(node_id, None)

        # Update membership
        if handle.member:
            handle.member.state = ClusterMemberState.FAILED
            try:
                await self._store.update(handle.member)
            except Exception:
                pass

        logger.warning("Node crashed: %s", node_id)

    async def restart_node(self, node_id: str) -> None:
        """
        Restart a crashed node: re-add to RPC routing and re-join membership.
        """
        handle = self._nodes.get(node_id)
        if handle is None:
            raise KeyError(f"Unknown node: {node_id}")

        cfg = handle.config
        peers = [c.node_id for c in self._configs if c.node_id != node_id]

        # Re-use the same routing logic as start()
        async def _rpc(target_id: str, method: str, payload: Any) -> Any:
            node = self._raft_nodes.get(target_id)
            if node is None:
                raise ConnectionRefusedError(f"Node {target_id} not found")
            if method == "request_vote":
                return await node.handle_vote_request(payload)
            elif method == "append_entries":
                return await node.handle_append_entries(payload)
            raise ValueError(f"Unknown method: {method}")

        new_raft = RaftNode(node_id=node_id, peers=peers, rpc_send=_rpc)
        self._raft_nodes[node_id] = new_raft
        handle.raft = new_raft
        handle._alive = True

        await new_raft.start()
        self._raft_tasks.extend(new_raft._tasks)

        # Re-register membership
        identity = NodeIdentity(
            node_id=cfg.node_id,
            host=cfg.host,
            port=cfg.port,
            metadata={"role": cfg.role_hint},
        )
        manager = ClusterMemberManager(self._store)
        try:
            member = await manager.join(identity)
            member.state = ClusterMemberState.RUNNING
            await self._store.update(member)
            handle.member = member
        except Exception:
            pass

        logger.info("Node restarted: %s", node_id)

    # ── Snapshots and introspection ───────────────────────────────────────────

    async def snapshot(self) -> ClusterSnapshot:
        """Return a point-in-time health snapshot of the cluster."""
        raft_roles = {
            nid: handle.raft.role.value
            for nid, handle in self._nodes.items()
        }

        members = await self._store.all()
        member_states = {m.node_id: m.state.value for m in members}

        leader = self.current_leader()
        leader_term = 0
        if leader and leader in self._nodes:
            leader_term = self._nodes[leader].raft.term

        profiles = await self._federator.profiles()
        cap_count = len(profiles)

        return ClusterSnapshot(
            taken_at=time.monotonic(),
            node_count=self._node_count,
            leader_id=leader,
            leader_term=leader_term,
            raft_roles=raft_roles,
            member_states=member_states,
            capability_count=cap_count,
        )

    def get_handle(self, node_id: str) -> NodeHandle:
        """Return the NodeHandle for a specific node."""
        if node_id not in self._nodes:
            raise KeyError(f"Unknown node: {node_id}")
        return self._nodes[node_id]

    def node_ids(self) -> list[str]:
        return list(self._nodes.keys())

    def alive_nodes(self) -> list[str]:
        return [nid for nid, h in self._nodes.items() if h.alive]

    async def propose(self, node_id: str, command: dict) -> bool:
        """Propose a command through a specific node's Raft instance."""
        handle = self._nodes.get(node_id)
        if handle is None or not handle.alive:
            raise KeyError(f"Node unavailable: {node_id}")
        return await handle.raft.propose(command)

    async def propose_via_leader(self, command: dict, timeout: float = 5.0) -> bool:
        """Propose a command via the current leader, waiting if needed."""
        leader = await self.wait_for_leader(timeout=timeout)
        return await self.propose(leader, command)
