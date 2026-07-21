"""
Phase 13 Sprint 3 — federation between two INDEPENDENT AEOS clusters.

Two clusters, each its own OS-level grpc.aio DomainServiceServer with a SEPARATE
KeyStore (separate trust roots, separate signing keys). Cluster A runs a task on
Cluster B:

    A --Handshake-->            B   (B mints a session token signed by B's key)
    A --DispatchFederatedTask-> B   (B verifies the session token with B's key,
                                     admits the task into B's own scheduler)
    A --GetTaskStatus-->        B   (the task really landed in B's registry)

What this proves that a single-cluster test cannot:
  - Cross-process, cross-trust-root dispatch works over a real socket.
  - The trust boundary is REAL and FAIL-CLOSED: a federation session token minted
    by cluster A's own signer is rejected by B (B only trusts tokens it issued),
    and a dispatch with no session token is denied. B never admits an
    unauthorized federated task.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

import grpc

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2, task_pb2_grpc, worker_pb2
from aeos.federation.v1 import federation_pb2, federation_pb2_grpc

from app.distributed.grpc.services import (
    DomainServiceServer,
    FederationServiceServicer,
    SchedulerServiceServicer,
)
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner, TokenVerifier


class _Cluster:
    """A standalone AEOS cluster: own keystore + Federation + Scheduler on a
    real grpc.aio server."""

    def __init__(self, keys_dir: str, cluster_id: str):
        self.cluster_id = cluster_id
        self.store = KeyStore(keys_dir=keys_dir, algorithm=KeyAlgorithm.ES256)
        self.store.initialize()
        self.signer = TokenSigner(self.store, issuer=cluster_id)
        self.verifier = TokenVerifier(self.store, issuer=cluster_id)
        self.scheduler = SchedulerServiceServicer(verifier=self.verifier)

        identity = federation_pb2.ClusterIdentity(
            cluster_id=cluster_id, display_name=cluster_id, region="us-east-1")
        self.federation = FederationServiceServicer(
            identity, self.signer, self.verifier,
            dispatch_fn=self._dispatch,
            capabilities_provider=lambda: [
                worker_pb2.WorkerCapabilities(
                    supported_task_types=["echo", "train"], max_concurrency=4)
            ],
            idle_worker_provider=lambda: 2,
        )
        self.server = (
            DomainServiceServer()
            .add_federation(self.federation)
            .add_scheduler(self.scheduler)
        )

    async def _dispatch(self, task) -> str:
        await self.scheduler.admit(task)
        return f"{self.cluster_id}-{task.task_id}"

    async def start(self):
        await self.server.start()
        self.channel = grpc.aio.insecure_channel(self.server.address)
        self.fed_stub = federation_pb2_grpc.FederationServiceStub(self.channel)
        self.sched_stub = task_pb2_grpc.SchedulerServiceStub(self.channel)

    async def stop(self):
        await self.channel.close()
        await self.server.stop()


@pytest.fixture
async def clusters(tmp_path):
    a = _Cluster(str(tmp_path / "a-keys"), "cluster-a")
    b = _Cluster(str(tmp_path / "b-keys"), "cluster-b")
    await a.start()
    await b.start()
    try:
        yield a, b
    finally:
        await a.stop()
        await b.stop()


async def _handshake(a: _Cluster, b: _Cluster) -> str:
    """A initiates a federation handshake with B; returns B's session token."""
    resp = await b.fed_stub.Handshake(federation_pb2.FederationHandshakeRequest(
        initiator=federation_pb2.ClusterIdentity(
            cluster_id=a.cluster_id, display_name=a.cluster_id),
        supported_algorithms=["ES256", "RS256"]))
    return resp.session_token


@pytest.mark.asyncio
async def test_cluster_a_runs_task_on_cluster_b(clusters):
    a, b = clusters
    session = await _handshake(a, b)
    assert session, "B issued no federation session token"

    # A dispatches a task to B, presenting B's session token.
    disp = await b.fed_stub.DispatchFederatedTask(federation_pb2.FederatedTaskRequest(
        task=task_pb2.Task(task_id="job-1", task_type="echo",
                           assigned_worker_id="b-worker-1"),
        originating_cluster_id=a.cluster_id,
        federation_session_token=session))
    assert disp.remote_task_id == "cluster-b-job-1"

    # The task really entered B's scheduler registry (queried over B's wire).
    st = await b.sched_stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="job-1"))
    assert st.worker_id == "b-worker-1"
    assert st.status == task_pb2.TASK_STATUS_ASSIGNED

    # A's OWN scheduler never saw it — this was executed on B, not locally.
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await a.sched_stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="job-1"))
    assert ei.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_capabilities_exchange_requires_session(clusters):
    a, b = clusters
    session = await _handshake(a, b)

    caps = await b.fed_stub.GetRemoteCapabilities(
        federation_pb2.GetRemoteCapabilitiesRequest(
            requesting_cluster_id=a.cluster_id, federation_session_token=session))
    assert caps.cluster_id == "cluster-b"
    assert caps.idle_workers == 2
    assert set(caps.available_task_types) == {"echo", "train"}

    # No session token → denied.
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await b.fed_stub.GetRemoteCapabilities(
            federation_pb2.GetRemoteCapabilitiesRequest(
                requesting_cluster_id=a.cluster_id))
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_foreign_token_is_rejected_cross_cluster(clusters):
    """The trust boundary is real: a session token A mints with A's OWN key is
    NOT accepted by B — B only honours tokens it issued itself (fail-closed)."""
    a, b = clusters

    # A forges a 'federation' token with A's signer (B never issued it).
    foreign = a.signer.sign("cluster-a", audience=["federation"], ttl_seconds=120)

    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await b.fed_stub.DispatchFederatedTask(federation_pb2.FederatedTaskRequest(
            task=task_pb2.Task(task_id="evil-1"),
            originating_cluster_id=a.cluster_id,
            federation_session_token=foreign))
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED

    # And nothing landed in B's scheduler.
    with pytest.raises(grpc.aio.AioRpcError) as ei2:
        await b.sched_stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="evil-1"))
    assert ei2.value.code() == grpc.StatusCode.NOT_FOUND
