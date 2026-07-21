"""
app/certification/measurements.py

REAL measurements against REAL AEOS seams. Nothing here fabricates a latency or
draws from a synthetic distribution — every number is a wall-clock observation of
an actual operation:

  * throughput + latency  → gRPC ``ScheduleTask`` calls, governance-gated, through
    a live ``SchedulerServiceServicer`` over a loopback ``grpc.aio`` channel.
  * failover              → a real in-process ``RaftNode`` cluster; the leader is
    crashed and we time how long until a NEW stable leader is elected.
  * recovery              → the real ``CheckpointEngine`` over a durable
    ``FileCheckpointStore``; we time reading back a committed checkpoint with a
    FRESH engine instance (models a new process taking over).
  * federation overhead   → a real A→B dispatch/execute/poll/verify round-trip
    (the Sprint 4 path), timed end to end.

Each measurement returns a ``MeasurementResult`` carrying the raw samples and
derived percentiles so the report layer never has to trust a pre-aggregated
number. The scale of each run comes from a ``ScaleSettings`` (dev-scale vs
full-scale) — the code path is identical regardless of scale.
"""

from __future__ import annotations

import asyncio
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim for aeos.* protos)

# Bind the protoc-rooted ``aeos.*`` namespace to the GENERATED package at import
# time. This must happen before anything else can bind the repo-root ``aeos`` SDK
# package (aeos.cli/sdk/workflow), which shares the top-level name — importing
# lazily inside functions loses that race under pytest's import ordering.
from aeos.core.v1 import task_pb2, task_pb2_grpc  # noqa: E402
from aeos.federation.v1 import federation_pb2, federation_pb2_grpc  # noqa: E402

from .profiles import ScaleSettings  # noqa: E402


# ── result container ────────────────────────────────────────────────────────

def _percentile(sorted_samples: list[float], p: float) -> float:
    if not sorted_samples:
        return 0.0
    idx = max(0, min(len(sorted_samples) - 1,
                     int(round(p / 100.0 * (len(sorted_samples) - 1)))))
    return sorted_samples[idx]


@dataclass
class MeasurementResult:
    """Raw samples + derived stats for one measurement dimension."""

    name: str
    unit: str
    samples: list[float] = field(default_factory=list)
    scalar: dict[str, float] = field(default_factory=dict)   # e.g. throughput_tps
    error_count: int = 0
    total_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        s = sorted(self.samples)
        stats = {}
        if s:
            stats = {
                "min": round(s[0], 3),
                "p50": round(_percentile(s, 50), 3),
                "p95": round(_percentile(s, 95), 3),
                "p99": round(_percentile(s, 99), 3),
                "max": round(s[-1], 3),
                "mean": round(statistics.mean(s), 3),
            }
        return {
            "name": self.name,
            "unit": self.unit,
            "sample_count": len(self.samples),
            "error_count": self.error_count,
            "total_count": self.total_count,
            "error_rate": (self.error_count / self.total_count) if self.total_count else 0.0,
            "stats": stats,
            "scalar": {k: round(v, 3) for k, v in self.scalar.items()},
            "notes": self.notes,
        }


# ── throughput + latency (real gRPC scheduler) ──────────────────────────────

async def measure_throughput_latency(scale: ScaleSettings) -> MeasurementResult:
    """Fire ``scale.throughput_tasks`` governance-gated ScheduleTask RPCs with
    up to ``scale.throughput_concurrency`` in flight; measure per-call latency
    and aggregate throughput. Real crypto verification runs on every call."""
    import grpc

    from app.distributed.grpc.services import (
        DomainServiceServer, SchedulerServiceServicer,
    )
    from app.security.key_rotation import KeyAlgorithm, KeyStore
    from app.security.token_verifier import TokenSigner, TokenVerifier

    res = MeasurementResult(name="throughput_latency", unit="ms")
    tmp = tempfile.mkdtemp(prefix="cert-tput-")
    store = KeyStore(keys_dir=tmp, algorithm=KeyAlgorithm.ES256)
    store.initialize()
    signer = TokenSigner(store, issuer="cert-cluster")
    verifier = TokenVerifier(store, issuer="cert-cluster")
    sched = SchedulerServiceServicer(verifier=verifier, require_governance=True)
    server = DomainServiceServer().add_scheduler(sched)
    await server.start()

    gov_token = signer.sign("cert-load", audience=["aeos"], ttl_seconds=3600,
                            gov_approved=True)
    channel = grpc.aio.insecure_channel(server.address)
    stub = task_pb2_grpc.SchedulerServiceStub(channel)

    sem = asyncio.Semaphore(scale.throughput_concurrency)
    latencies: list[float] = []
    errors = 0

    async def _submit(i: int) -> None:
        nonlocal errors
        task = task_pb2.Task(task_id=f"cert-task-{i}", task_type="echo",
                             governance_token=gov_token)
        req = task_pb2.ScheduleTaskRequest(task=task, require_governance=True)
        async with sem:
            t0 = time.perf_counter()
            try:
                await stub.ScheduleTask(req)
                latencies.append((time.perf_counter() - t0) * 1000.0)
            except Exception:
                errors += 1

    wall0 = time.perf_counter()
    await asyncio.gather(*[_submit(i) for i in range(scale.throughput_tasks)])
    wall = time.perf_counter() - wall0

    await channel.close()
    await server.stop()

    res.samples = latencies
    res.total_count = scale.throughput_tasks
    res.error_count = errors
    completed = len(latencies)
    res.scalar["throughput_tps"] = (completed / wall) if wall > 0 else 0.0
    res.scalar["wall_seconds"] = wall
    res.notes.append(
        f"governance-gated gRPC ScheduleTask; {completed}/{scale.throughput_tasks} "
        f"completed; concurrency={scale.throughput_concurrency}")
    return res


# ── failover (real Raft election) ───────────────────────────────────────────

class _CertRaftCluster:
    """Minimal in-process Raft cluster with a controllable transport — enough to
    crash the leader and time re-election. Mirrors the Sprint 3 RoutableCluster."""

    _FAST = dict(heartbeat_ms=20, election_min_ms=60, election_max_ms=120)

    def __init__(self, node_ids: list[str]):
        from app.distributed.consensus.raft import RaftNode
        self.ids = list(node_ids)
        self._alive: set[str] = set(node_ids)
        self.nodes = {nid: RaftNode(nid, [p for p in node_ids if p != nid],
                                    self._make_rpc(nid), **self._FAST)
                      for nid in node_ids}

    def _make_rpc(self, src: str):
        async def rpc_send(dst: str, method: str, payload):
            if src not in self._alive or dst not in self._alive:
                raise ConnectionError(f"{src}->{dst}: down")
            target = self.nodes.get(dst)
            if target is None:
                raise ConnectionError(f"{dst}: gone")
            if method == "request_vote":
                return await target.handle_vote_request(payload)
            if method == "append_entries":
                return await target.handle_append_entries(payload)
            raise ValueError(method)
        return rpc_send

    async def start(self):
        await asyncio.gather(*(n.start() for n in self.nodes.values()))

    async def stop(self):
        await asyncio.gather(*(n.stop() for n in self.nodes.values()),
                             return_exceptions=True)

    async def crash(self, nid: str):
        self._alive.discard(nid)
        await self.nodes[nid].stop()

    def _leaders(self):
        from app.distributed.consensus.raft import RaftRole
        return [self.nodes[i] for i in self.ids
                if i in self._alive and self.nodes[i].role == RaftRole.LEADER]

    def stable_leader(self):
        ls = self._leaders()
        if len(ls) != 1:
            return None
        top = max(self.nodes[i].term for i in self.ids if i in self._alive)
        return ls[0] if ls[0].term == top else None


async def _wait_stable_leader(cluster: _CertRaftCluster, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ldr = cluster.stable_leader()
        if ldr is not None:
            return ldr
        await asyncio.sleep(0.005)
    return None


async def measure_failover(scale: ScaleSettings) -> MeasurementResult:
    """Elect a leader, crash it, time until a NEW stable leader emerges. Repeat
    ``scale.failover_trials`` times on a real ``scale.failover_nodes``-node Raft
    cluster. Every election is real (real votes over a real transport)."""
    res = MeasurementResult(name="failover", unit="ms")
    node_ids = [f"n{i}" for i in range(scale.failover_nodes)]

    for trial in range(scale.failover_trials):
        cluster = _CertRaftCluster(node_ids)
        res.total_count += 1
        try:
            await cluster.start()
            leader = await _wait_stable_leader(cluster)
            if leader is None:
                res.error_count += 1
                res.notes.append(f"trial {trial}: no initial leader")
                continue
            old_id = leader._id  # noqa: SLF001 (id for crash target)
            t0 = time.monotonic()
            await cluster.crash(old_id)
            new_leader = await _wait_stable_leader(cluster)
            if new_leader is None or new_leader._id == old_id:  # noqa: SLF001
                res.error_count += 1
                res.notes.append(f"trial {trial}: no re-election after crash")
                continue
            res.samples.append((time.monotonic() - t0) * 1000.0)
        finally:
            await cluster.stop()

    res.notes.append(
        f"{scale.failover_nodes}-node in-process Raft; leader-crash → re-election "
        f"time over {scale.failover_trials} trials")
    return res


# ── recovery (real checkpoint read-back) ────────────────────────────────────

async def measure_recovery(scale: ScaleSettings) -> MeasurementResult:
    """Write+commit ``scale.recovery_checkpoints`` durable checkpoints, then time
    reading each back with a FRESH ``CheckpointEngine`` (models a new process
    resuming). Uses the real engine over a durable ``FileCheckpointStore``."""
    from app.distributed.execution.checkpoint import CheckpointEngine
    from app.distributed.execution.context import CheckpointData, ExecutionState
    from app.distributed.testbed.file_checkpoint_store import FileCheckpointStore

    res = MeasurementResult(name="recovery", unit="ms")
    root = Path(tempfile.mkdtemp(prefix="cert-recovery-"))

    # Phase 1: durably write + commit N checkpoints.
    writer_store = FileCheckpointStore(root)
    writer = CheckpointEngine(writer_store)
    ids: list[tuple[str, str]] = []
    for i in range(scale.recovery_checkpoints):
        wf, step = f"wf-{i}", f"step-{i}"
        data = CheckpointData(
            task_id=f"t-{i}", workflow_id=wf, step_id=step,
            execution_id=f"e-{i}", state=ExecutionState.RUNNING,
            step_index=i, total_steps=scale.recovery_checkpoints,
            worker_id="cert-worker", fencing_token=i,
        )
        entry = await writer.write_full(data)
        await writer.commit(entry)
        ids.append((wf, step))

    # Phase 2: read each back with a FRESH engine/store (cold read from disk).
    for wf, step in ids:
        reader = CheckpointEngine(FileCheckpointStore(root))
        res.total_count += 1
        t0 = time.perf_counter()
        loaded = await reader.load(wf, step)
        dt = (time.perf_counter() - t0) * 1000.0
        if loaded is None:
            res.error_count += 1
        else:
            res.samples.append(dt)

    res.notes.append(
        f"real CheckpointEngine over durable FileCheckpointStore; cold read-back "
        f"of {scale.recovery_checkpoints} committed checkpoints with fresh engine")
    return res


# ── federation overhead (real A→B round-trip) ───────────────────────────────

async def measure_federation_overhead(scale: ScaleSettings) -> MeasurementResult:
    """Time full A-side round-trips: dispatch → B executes → poll → verify
    evidence, over ``scale.federation_samples`` samples. This is the Sprint 4
    trust path, unmodified."""
    import grpc

    from aeos.federation.v1 import federation_pb2_grpc
    from app.distributed.grpc.services import (
        DomainServiceServer, FederatedExecutor, FederationClient,
        FederationServiceServicer, SchedulerServiceServicer, extract_jti,
        make_echo_executor,
    )
    from app.security.jwks import JWKSProvider, verifier_from_jwks
    from app.security.key_rotation import KeyAlgorithm, KeyStore
    from app.security.token_verifier import TokenSigner, TokenVerifier

    res = MeasurementResult(name="federation_overhead", unit="ms")
    tmp = Path(tempfile.mkdtemp(prefix="cert-fed-"))

    def _cluster(cid: str, executing: bool):
        store = KeyStore(keys_dir=str(tmp / cid), algorithm=KeyAlgorithm.ES256)
        store.initialize()
        signer = TokenSigner(store, issuer=cid)
        verifier = TokenVerifier(store, issuer=cid)
        sched = SchedulerServiceServicer(verifier=verifier)
        executor = None
        ident = federation_pb2.ClusterIdentity(cluster_id=cid, display_name=cid,
                                               region="cert")
        if executing:
            executor = FederatedExecutor(cluster_id=cid, signer=signer,
                                         execute_fn=make_echo_executor("b-worker-1"),
                                         scheduler=sched)
        fed = FederationServiceServicer(ident, signer, verifier, executor=executor)
        server = DomainServiceServer().add_federation(fed).add_scheduler(sched)
        return store, signer, server

    _, sig_a, server_a = _cluster("cluster-a", executing=False)
    store_b, _, server_b = _cluster("cluster-b", executing=True)
    await server_a.start()
    await server_b.start()

    channel = grpc.aio.insecure_channel(server_b.address)
    stub = federation_pb2_grpc.FederationServiceStub(channel)
    client = FederationClient(stub, originating_cluster_id="cluster-a")
    peer_verifier = verifier_from_jwks(JWKSProvider(store_b).jwks_dict(),
                                       issuer="cluster-b")
    session = await client.handshake(
        federation_pb2.ClusterIdentity(cluster_id="cluster-a"))

    for i in range(scale.federation_samples):
        tid = f"fed-{i}"
        tok = sig_a.sign(tid, audience=["aeos"], ttl_seconds=300, gov_approved=True)
        jti = extract_jti(tok)
        task = task_pb2.Task(task_id=tid, task_type="echo",
                             assigned_worker_id="b-worker-1", governance_token=tok)
        res.total_count += 1
        t0 = time.perf_counter()
        try:
            rid = await client.dispatch(task, session)
            resp = await client.await_result(rid, session, timeout=5.0)
            FederationClient.verify_evidence(
                resp, peer_verifier=peer_verifier, expected_governance_jti=jti,
                expected_executing_cluster="cluster-b",
                expected_originating_cluster="cluster-a")
            res.samples.append((time.perf_counter() - t0) * 1000.0)
        except Exception:
            res.error_count += 1

    await channel.close()
    await server_a.stop()
    await server_b.stop()

    res.notes.append(
        f"full A round-trip dispatch→execute→poll→verify over "
        f"{scale.federation_samples} samples (echo executor; loopback)")
    return res
