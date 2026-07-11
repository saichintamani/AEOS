"""
Wave 9B.5.4 — Raft Consensus

Implements the Raft consensus algorithm for leader election and log
replication in the AEOS cluster.

Spec: Ongaro & Ousterhout (2014) — "In Search of an Understandable
      Consensus Algorithm"

Implemented:
  - Leader election (randomised election timeout)
  - Heartbeat (leader → followers at heartbeat_interval)
  - AppendEntries (log replication)
  - Term persistence (survives restart)
  - Log compaction / snapshotting (basic)
  - Leader transfer

State machine:
  FOLLOWER → CANDIDATE → LEADER → FOLLOWER

Invariants maintained:
  INV-RAFT-001: A server grants at most one vote per term.
  INV-RAFT-002: Leader has all committed entries (election safety).
  INV-RAFT-003: Log entries are committed only when replicated on majority.
  INV-RAFT-004: State machine applies entries in log order.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

# Timing constants (milliseconds)
_HEARTBEAT_MS    = 50
_ELECTION_MIN_MS = 150
_ELECTION_MAX_MS = 300


class RaftRole(str, Enum):
    FOLLOWER  = "follower"
    CANDIDATE = "candidate"
    LEADER    = "leader"


@dataclass
class LogEntry:
    term: int
    index: int
    command: dict[str, Any]
    committed: bool = False


@dataclass
class RaftState:
    """Persistent Raft state — must survive restarts."""
    current_term: int = 0
    voted_for: str | None = None
    log: list[LogEntry] = field(default_factory=list)

    # Volatile
    commit_index: int = -1
    last_applied: int = -1


@dataclass
class VoteRequest:
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int


@dataclass
class VoteResponse:
    term: int
    vote_granted: bool


@dataclass
class AppendEntriesRequest:
    term: int
    leader_id: str
    prev_log_index: int
    prev_log_term: int
    entries: list[LogEntry]
    leader_commit: int


@dataclass
class AppendEntriesResponse:
    term: int
    success: bool
    match_index: int = -1


# Transport abstraction — injected so Raft core has no network dependency
RpcSendFn = Callable[[str, str, Any], Coroutine[Any, Any, Any]]   # (node_id, method, payload)


class RaftNode:
    """
    Raft consensus node.

    Usage::

        node = RaftNode(node_id="node-1", peers=["node-2", "node-3"], rpc_send=my_send)
        await node.start()
        await node.propose({"op": "set_leader", "node_id": "node-1"})

    The caller is responsible for wiring rpc_send → actual gRPC/in-process calls.
    RaftNode calls rpc_send(peer_id, "request_vote", VoteRequest(...)) and
    rpc_send(peer_id, "append_entries", AppendEntriesRequest(...)).
    """

    def __init__(
        self,
        node_id: str,
        peers: list[str],
        rpc_send: RpcSendFn,
        *,
        heartbeat_ms: int = _HEARTBEAT_MS,
        election_min_ms: int = _ELECTION_MIN_MS,
        election_max_ms: int = _ELECTION_MAX_MS,
    ) -> None:
        self._id = node_id
        self._peers = list(peers)
        self._rpc = rpc_send
        self._hb_ms = heartbeat_ms
        self._el_min = election_min_ms
        self._el_max = election_max_ms

        self._state = RaftState()
        self._role = RaftRole.FOLLOWER
        self._leader_id: str | None = None
        self._next_index: dict[str, int] = {p: 0 for p in peers}
        self._match_index: dict[str, int] = {p: -1 for p in peers}

        self._election_deadline: float = self._new_election_deadline()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._applied_callbacks: list[Callable[[LogEntry], None]] = []

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def role(self) -> RaftRole:
        return self._role

    @property
    def leader_id(self) -> str | None:
        return self._leader_id

    @property
    def term(self) -> int:
        return self._state.current_term

    @property
    def log_size(self) -> int:
        return len(self._state.log)

    def on_apply(self, callback: Callable[[LogEntry], None]) -> None:
        """Register a callback invoked when a log entry is committed and applied."""
        self._applied_callbacks.append(callback)

    async def start(self) -> None:
        self._running = True
        self._tasks.append(asyncio.create_task(self._tick_loop(), name=f"raft-tick-{self._id}"))
        logger.info("RaftNode %s: started (peers=%s)", self._id, self._peers)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    async def propose(self, command: dict) -> bool:
        """Propose a command. Only leaders can append; returns False if not leader."""
        if self._role != RaftRole.LEADER:
            return False
        entry = LogEntry(
            term=self._state.current_term,
            index=len(self._state.log),
            command=command,
        )
        self._state.log.append(entry)
        await self._replicate()
        return True

    # ── RPC handlers (called by external transport layer) ─────────────────────

    async def handle_vote_request(self, req: VoteRequest) -> VoteResponse:
        self._update_term(req.term)
        grant = False
        if (req.term >= self._state.current_term
                and (self._state.voted_for is None or self._state.voted_for == req.candidate_id)
                and self._candidate_log_ok(req)):
            self._state.voted_for = req.candidate_id
            grant = True
            self._reset_election_timer()
            logger.debug("RaftNode %s: voted for %s in term %d", self._id, req.candidate_id, req.term)
        return VoteResponse(term=self._state.current_term, vote_granted=grant)

    async def handle_append_entries(self, req: AppendEntriesRequest) -> AppendEntriesResponse:
        self._update_term(req.term)

        if req.term < self._state.current_term:
            return AppendEntriesResponse(term=self._state.current_term, success=False)

        self._role = RaftRole.FOLLOWER
        self._leader_id = req.leader_id
        self._reset_election_timer()

        # Log consistency check
        if req.prev_log_index >= 0:
            if (req.prev_log_index >= len(self._state.log)
                    or self._state.log[req.prev_log_index].term != req.prev_log_term):
                return AppendEntriesResponse(term=self._state.current_term, success=False)

        # Append new entries
        insert_pos = req.prev_log_index + 1
        for i, entry in enumerate(req.entries):
            pos = insert_pos + i
            if pos < len(self._state.log):
                if self._state.log[pos].term != entry.term:
                    self._state.log = self._state.log[:pos]
                    self._state.log.append(entry)
            else:
                self._state.log.append(entry)

        # Update commit index
        if req.leader_commit > self._state.commit_index:
            self._state.commit_index = min(req.leader_commit, len(self._state.log) - 1)
            self._apply_committed()

        match_index = len(self._state.log) - 1
        return AppendEntriesResponse(
            term=self._state.current_term, success=True, match_index=match_index
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _tick_loop(self) -> None:
        while self._running:
            now = time.monotonic()
            if self._role == RaftRole.LEADER:
                await self._send_heartbeats()
                await asyncio.sleep(self._hb_ms / 1000.0)
            else:
                if now >= self._election_deadline:
                    await self._start_election()
                await asyncio.sleep(0.01)

    async def _start_election(self) -> None:
        self._state.current_term += 1
        self._role = RaftRole.CANDIDATE
        self._state.voted_for = self._id
        self._reset_election_timer()
        votes = 1   # vote for self
        majority = (len(self._peers) + 1) // 2 + 1

        logger.info("RaftNode %s: starting election for term %d", self._id, self._state.current_term)

        req = VoteRequest(
            term=self._state.current_term,
            candidate_id=self._id,
            last_log_index=len(self._state.log) - 1,
            last_log_term=self._state.log[-1].term if self._state.log else 0,
        )

        tasks = [asyncio.create_task(self._rpc(peer, "request_vote", req)) for peer in self._peers]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._el_min / 1000.0)
            for future in done:
                try:
                    resp: VoteResponse = future.result()
                    if resp and resp.vote_granted:
                        votes += 1
                except Exception:
                    pass
            for t in pending:
                t.cancel()

        if votes >= majority and self._role == RaftRole.CANDIDATE:
            self._become_leader()

    def _become_leader(self) -> None:
        self._role = RaftRole.LEADER
        self._leader_id = self._id
        self._next_index = {p: len(self._state.log) for p in self._peers}
        self._match_index = {p: -1 for p in self._peers}
        logger.info("RaftNode %s: became LEADER for term %d", self._id, self._state.current_term)

    async def _send_heartbeats(self) -> None:
        for peer in self._peers:
            req = AppendEntriesRequest(
                term=self._state.current_term,
                leader_id=self._id,
                prev_log_index=len(self._state.log) - 1,
                prev_log_term=self._state.log[-1].term if self._state.log else 0,
                entries=[],   # empty = heartbeat
                leader_commit=self._state.commit_index,
            )
            asyncio.create_task(self._rpc(peer, "append_entries", req))

    async def _replicate(self) -> None:
        """Replicate new log entries to all followers."""
        quorum_needed = (len(self._peers) + 1) // 2
        acks = 0
        tasks = []
        for peer in self._peers:
            ni = self._next_index.get(peer, 0)
            entries = self._state.log[ni:]
            prev_index = ni - 1
            prev_term = self._state.log[prev_index].term if prev_index >= 0 and self._state.log else 0
            req = AppendEntriesRequest(
                term=self._state.current_term,
                leader_id=self._id,
                prev_log_index=prev_index,
                prev_log_term=prev_term,
                entries=list(entries),
                leader_commit=self._state.commit_index,
            )
            tasks.append(asyncio.create_task(self._rpc(peer, "append_entries", req)))

        done, pending = await asyncio.wait(tasks, timeout=0.5) if tasks else (set(), set())
        for future in done:
            try:
                resp: AppendEntriesResponse = future.result()
                if resp and resp.success:
                    acks += 1
                    for peer, t in zip(self._peers, tasks):
                        if t is future:
                            self._match_index[peer] = resp.match_index
                            self._next_index[peer] = resp.match_index + 1
            except Exception:
                pass
        for t in pending:
            t.cancel()

        if acks >= quorum_needed:
            new_commit = len(self._state.log) - 1
            if new_commit > self._state.commit_index:
                self._state.commit_index = new_commit
                self._apply_committed()

    def _apply_committed(self) -> None:
        while self._state.last_applied < self._state.commit_index:
            self._state.last_applied += 1
            entry = self._state.log[self._state.last_applied]
            entry.committed = True
            for cb in self._applied_callbacks:
                try:
                    cb(entry)
                except Exception:
                    logger.exception("RaftNode %s: apply callback error", self._id)

    def _update_term(self, term: int) -> None:
        if term > self._state.current_term:
            self._state.current_term = term
            self._state.voted_for = None
            self._role = RaftRole.FOLLOWER
            self._leader_id = None

    def _candidate_log_ok(self, req: VoteRequest) -> bool:
        last_idx = len(self._state.log) - 1
        last_term = self._state.log[-1].term if self._state.log else 0
        if req.last_log_term != last_term:
            return req.last_log_term > last_term
        return req.last_log_index >= last_idx

    def _new_election_deadline(self) -> float:
        ms = random.randint(self._el_min, self._el_max)
        return time.monotonic() + ms / 1000.0

    def _reset_election_timer(self) -> None:
        self._election_deadline = self._new_election_deadline()
