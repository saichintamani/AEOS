"""
Phase 13 Sprint 4 — REMOTE federated execution, end to end.

Sprint 3 proved the federation *trust boundary* (A dispatches to B, B admits into
its registry, foreign tokens rejected). It stopped at admission. This suite proves
the full loop that makes federation a real capability rather than a handshake:

    A --Handshake-->              B
    A --DispatchFederatedTask-->  B   (B ADMITS + EXECUTES on its own worker seam)
    A --GetFederatedTaskResult--> B   (B returns the TaskResult + SIGNED evidence)
    A verifies B's evidence against B's PUBLISHED JWKS, checks the result hash,
      the governance jti it minted, and both cluster identities — fail-closed.

The trust asymmetry is the load-bearing property: B signs execution evidence with
a private key A never holds; A verifies with B's public JWKS (reconstructed via
``verifier_from_jwks`` — the real import path, no shared secret, no private-key
transfer). A cannot forge B's evidence; B cannot swap a result after signing
without breaking the hash binding.

Required failure scenarios (all fail-closed / non-silent), per the sprint directive:
  - remote cluster unavailable        → dispatch raises a gRPC UNAVAILABLE error
  - invalid signature                 → FederationTrustError
  - expired (session) token           → PERMISSION_DENIED, no execution
  - federation timeout                → await_result raises TimeoutError
  - network partition mid-flight      → GetFederatedTaskResult raises a gRPC error
  - duplicate result delivery         → identical signed evidence; executed once

Marked to require grpc + cryptography.

Phase: 13 Sprint 4
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

import grpc

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2, task_pb2_grpc
from aeos.federation.v1 import federation_pb2, federation_pb2_grpc

from app.distributed.grpc.services import (
    DomainServiceServer,
    FederatedExecutor,
    FederationClient,
    FederationServiceServicer,
    FederationTrustError,
    SchedulerServiceServicer,
    extract_jti,
    make_echo_executor,
)
from app.security.jwks import JWKSProvider, verifier_from_jwks
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner, TokenVerifier


class _Cluster:
    """A standalone AEOS cluster. When ``executing`` is set it hosts a real
    FederatedExecutor over its own scheduler + worker-runtime seam."""

    def __init__(self, keys_dir: str, cluster_id: str, *, executing: bool = False,
                 exec_delay: float = 0.0, worker_id: str = "b-worker-1"):
        self.cluster_id = cluster_id
        self.store = KeyStore(keys_dir=keys_dir, algorithm=KeyAlgorithm.ES256)
        self.store.initialize()
        self.signer = TokenSigner(self.store, issuer=cluster_id)
        self.verifier = TokenVerifier(self.store, issuer=cluster_id)
        self.scheduler = SchedulerServiceServicer(verifier=self.verifier)
        self.executor = None
        identity = federation_pb2.ClusterIdentity(
            cluster_id=cluster_id, display_name=cluster_id, region="us-east-1")
        if executing:
            self.executor = FederatedExecutor(
                cluster_id=cluster_id, signer=self.signer,
                execute_fn=make_echo_executor(worker_id, delay_s=exec_delay),
                scheduler=self.scheduler)
        self.federation = FederationServiceServicer(
            identity, self.signer, self.verifier, executor=self.executor)
        self.server = (DomainServiceServer()
                       .add_federation(self.federation)
                       .add_scheduler(self.scheduler))
        self._stopped = False

    async def start(self):
        await self.server.start()

    async def stop(self):
        if self._stopped:
            return
        self._stopped = True
        await self.server.stop()

    def jwks(self) -> dict:
        return JWKSProvider(self.store).jwks_dict()


class _Harness:
    def __init__(self, tmp_path):
        self._tmp = tmp_path
        self._clusters: list[_Cluster] = []
        self._channels: list = []
        self._n = 0

    async def cluster(self, cluster_id: str, **kw) -> _Cluster:
        c = _Cluster(str(self._tmp / f"{cluster_id}-{self._n}"), cluster_id, **kw)
        self._n += 1
        await c.start()
        self._clusters.append(c)
        return c

    def channel_to(self, b: _Cluster):
        ch = grpc.aio.insecure_channel(b.server.address)
        self._channels.append(ch)
        return ch

    def client_to(self, b: _Cluster, originating_cluster_id: str) -> FederationClient:
        stub = federation_pb2_grpc.FederationServiceStub(self.channel_to(b))
        return FederationClient(stub, originating_cluster_id=originating_cluster_id)

    def peer_verifier(self, b: _Cluster) -> TokenVerifier:
        # Models A fetching B's /.well-known/jwks.json and building a verifier.
        return verifier_from_jwks(b.jwks(), issuer=b.cluster_id)

    async def close(self):
        for ch in self._channels:
            try:
                await ch.close()
            except Exception:
                pass
        for c in self._clusters:
            try:
                await c.stop()
            except Exception:
                pass


@pytest.fixture
async def h(tmp_path):
    harness = _Harness(tmp_path)
    try:
        yield harness
    finally:
        await harness.close()


def _gov_token(a: _Cluster, task_id: str) -> tuple[str, str]:
    """A mints a governance token authorizing task_id; return (token, jti)."""
    token = a.signer.sign(task_id, audience=["aeos"], ttl_seconds=300, gov_approved=True)
    return token, extract_jti(token)


def _task(task_id: str, gov_token: str, worker_id: str = "b-worker-1") -> "task_pb2.Task":
    return task_pb2.Task(task_id=task_id, task_type="echo",
                         assigned_worker_id=worker_id, governance_token=gov_token)


# ── happy path: full loop + trust verification + measured overhead ─────────────

@pytest.mark.asyncio
async def test_a_executes_on_b_and_verifies_signed_result(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")

    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    assert session

    gov_token, gov_jti = _gov_token(a, "job-1")
    task = _task("job-1", gov_token)

    t0 = time.monotonic()
    remote_id = await client.dispatch(task, session)
    assert remote_id == "cluster-b-job-1"

    resp = await client.await_result(remote_id, session, timeout=5.0)
    overhead_s = time.monotonic() - t0

    # A verifies B's signed evidence against B's JWKS + all bindings (fail-closed).
    ev = FederationClient.verify_evidence(
        resp, peer_verifier=h.peer_verifier(b),
        expected_governance_jti=gov_jti,
        expected_executing_cluster="cluster-b",
        expected_originating_cluster="cluster-a")

    assert resp.result.status == task_pb2.TASK_STATUS_SUCCEEDED
    assert resp.result.worker_id == "b-worker-1"
    assert ev.executing_cluster_id == "cluster-b"
    assert ev.governance_jti == gov_jti

    # The task really entered B's OWN scheduler registry with the recorded result.
    sched = task_pb2_grpc.SchedulerServiceStub(h.channel_to(b))
    st = await sched.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="job-1"))
    assert st.status == task_pb2.TASK_STATUS_SUCCEEDED
    assert st.result.worker_id == "b-worker-1"

    # Round-trip (dispatch → execute → poll → verify) is well under budget locally.
    assert overhead_s < 5.0, f"federation round-trip took {overhead_s:.3f}s"


# ── trust chain negatives ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tampered_evidence_signature_is_rejected(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, gov_jti = _gov_token(a, "job-2")
    remote_id = await client.dispatch(_task("job-2", gov_token), session)
    resp = await client.await_result(remote_id, session)

    # Flip a byte of the signed evidence token → signature no longer verifies.
    resp.evidence.evidence_token = resp.evidence.evidence_token[:-2] + "AA"
    with pytest.raises(FederationTrustError, match="signature|rejected"):
        FederationClient.verify_evidence(
            resp, peer_verifier=h.peer_verifier(b),
            expected_governance_jti=gov_jti,
            expected_executing_cluster="cluster-b",
            expected_originating_cluster="cluster-a")


@pytest.mark.asyncio
async def test_tampered_result_breaks_hash_binding(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, gov_jti = _gov_token(a, "job-3")
    remote_id = await client.dispatch(_task("job-3", gov_token), session)
    resp = await client.await_result(remote_id, session)

    # Rewrite the result AFTER B signed it → recomputed hash ≠ signed hash.
    resp.result.worker_id = "attacker-worker"
    with pytest.raises(FederationTrustError, match="hash"):
        FederationClient.verify_evidence(
            resp, peer_verifier=h.peer_verifier(b),
            expected_governance_jti=gov_jti,
            expected_executing_cluster="cluster-b",
            expected_originating_cluster="cluster-a")


@pytest.mark.asyncio
async def test_wrong_governance_jti_is_rejected(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, _ = _gov_token(a, "job-4")
    remote_id = await client.dispatch(_task("job-4", gov_token), session)
    resp = await client.await_result(remote_id, session)

    # A checks against a DIFFERENT authorization than the one B executed under.
    with pytest.raises(FederationTrustError, match="governance jti"):
        FederationClient.verify_evidence(
            resp, peer_verifier=h.peer_verifier(b),
            expected_governance_jti="some-other-jti",
            expected_executing_cluster="cluster-b",
            expected_originating_cluster="cluster-a")


@pytest.mark.asyncio
async def test_evidence_verified_with_wrong_peer_key_is_rejected(h):
    """Evidence signed by B must NOT verify against a different cluster's JWKS —
    the cross-cluster key binding is real."""
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    other = await h.cluster("cluster-b", executing=True)  # same id, different keys
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, gov_jti = _gov_token(a, "job-5")
    remote_id = await client.dispatch(_task("job-5", gov_token), session)
    resp = await client.await_result(remote_id, session)

    # Verify B's evidence against a DIFFERENT cluster's JWKS (kid absent) → reject.
    with pytest.raises(FederationTrustError):
        FederationClient.verify_evidence(
            resp, peer_verifier=h.peer_verifier(other),
            expected_governance_jti=gov_jti,
            expected_executing_cluster="cluster-b",
            expected_originating_cluster="cluster-a")


# ── required failure scenarios ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remote_cluster_unavailable(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, _ = _gov_token(a, "job-6")

    await b.stop()  # remote cluster goes down before dispatch

    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await client.dispatch(_task("job-6", gov_token), session)
    assert ei.value.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


@pytest.mark.asyncio
async def test_expired_session_token_denied(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    gov_token, _ = _gov_token(a, "job-7")

    # An expired federation session token (past the verifier's clock skew).
    expired = b.signer.sign("cluster-a", audience=["federation"], ttl_seconds=-120)

    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await client.dispatch(_task("job-7", gov_token), expired)
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED

    # And nothing executed → the task never entered B's registry.
    sched = task_pb2_grpc.SchedulerServiceStub(h.channel_to(b))
    with pytest.raises(grpc.aio.AioRpcError) as ei2:
        await sched.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="job-7"))
    assert ei2.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_federation_result_timeout(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True, exec_delay=2.0)  # slow execution
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, _ = _gov_token(a, "job-8")
    remote_id = await client.dispatch(_task("job-8", gov_token), session)

    with pytest.raises(TimeoutError):
        await client.await_result(remote_id, session, timeout=0.3)


@pytest.mark.asyncio
async def test_network_partition_mid_flight(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True, exec_delay=1.0)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, _ = _gov_token(a, "job-9")
    remote_id = await client.dispatch(_task("job-9", gov_token), session)

    # Partition: B becomes unreachable while the result is still being produced.
    await b.stop()
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await client.await_result(remote_id, session, timeout=2.0)
    assert ei.value.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED)


@pytest.mark.asyncio
async def test_duplicate_result_delivery_is_idempotent(h):
    a = await h.cluster("cluster-a")
    b = await h.cluster("cluster-b", executing=True)
    client = h.client_to(b, "cluster-a")
    session = await client.handshake(federation_pb2.ClusterIdentity(cluster_id="cluster-a"))
    gov_token, gov_jti = _gov_token(a, "job-10")

    # Dispatch twice with the same task id → same remote id, executed once.
    remote_id1 = await client.dispatch(_task("job-10", gov_token), session)
    remote_id2 = await client.dispatch(_task("job-10", gov_token), session)
    assert remote_id1 == remote_id2

    # Poll twice → byte-identical signed evidence both times (safe to redeliver).
    r1 = await client.await_result(remote_id1, session)
    r2 = await client.await_result(remote_id1, session)
    assert r1.evidence.evidence_token == r2.evidence.evidence_token
    assert r1.evidence.result_hash == r2.evidence.result_hash

    # Verification succeeds and A can dedupe by the evidence jti.
    ev = FederationClient.verify_evidence(
        r1, peer_verifier=h.peer_verifier(b),
        expected_governance_jti=gov_jti,
        expected_executing_cluster="cluster-b",
        expected_originating_cluster="cluster-a")
    assert ev.task_id == "job-10"
