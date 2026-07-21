"""
Phase 13 Sprint 3 — domain gRPC servicers over real sockets.

Exercises all five AEOS domain services (Governance, Scheduler, Worker,
Observability, Federation) hosted together on ONE grpc.aio DomainServiceServer,
driven by real client stubs over an ephemeral localhost port. No mocks: tokens
are really signed/verified with ES256, tasks really enter the scheduler
registry, workers really land in the WorkerPool.

The load-bearing assertions are the FAIL-CLOSED ones — an unsigned task is
rejected by the scheduler, a tampered token fails verification, a federated
dispatch without a session token is denied — because those are the properties
that make the distribution trustworthy, not merely functional.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

import grpc

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2, task_pb2_grpc, worker_pb2, worker_pb2_grpc
from aeos.federation.v1 import federation_pb2, federation_pb2_grpc
from aeos.governance.v1 import governance_pb2, governance_pb2_grpc
from aeos.observability.v1 import observability_pb2, observability_pb2_grpc

from app.distributed.grpc.services import (
    DomainServiceServer,
    FederationServiceServicer,
    GovernanceServiceServicer,
    ObservabilityServiceServicer,
    SchedulerServiceServicer,
    WorkerServiceServicer,
)
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner, TokenVerifier


async def _wait(pred, timeout=5.0, interval=0.02):
    for _ in range(int(timeout / interval)):
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


class _Cluster:
    """One DomainServiceServer hosting all five servicers + shared crypto."""

    def __init__(self, keys_dir: str, cluster_id: str = "aeos"):
        self.store = KeyStore(keys_dir=keys_dir, algorithm=KeyAlgorithm.ES256)
        self.store.initialize()
        self.signer = TokenSigner(self.store, issuer=cluster_id)
        self.verifier = TokenVerifier(self.store, issuer=cluster_id)

        self.governance = GovernanceServiceServicer(
            self.signer, self.verifier, issuing_cluster_id=cluster_id)
        self.worker = WorkerServiceServicer()
        self.scheduler = SchedulerServiceServicer(
            verifier=self.verifier, require_governance=False)

        identity = federation_pb2.ClusterIdentity(
            cluster_id=cluster_id, display_name=cluster_id, region="us-east-1")
        self.federation = FederationServiceServicer(
            identity, self.signer, self.verifier,
            dispatch_fn=self._federated_dispatch)
        self.observability = ObservabilityServiceServicer()

        self.server = (
            DomainServiceServer()
            .add_governance(self.governance)
            .add_scheduler(self.scheduler)
            .add_worker(self.worker)
            .add_observability(self.observability)
            .add_federation(self.federation)
        )

    async def _federated_dispatch(self, task) -> str:
        # Route the federated task into THIS cluster's real scheduler registry.
        await self.scheduler.admit(task)
        return f"remote-{task.task_id}"

    async def start(self):
        await self.server.start()
        self.channel = grpc.aio.insecure_channel(self.server.address)

    async def stop(self):
        await self.channel.close()
        await self.server.stop()

    def sign_task_token(self, subject: str, ttl=120) -> str:
        return self.signer.sign(subject, audience=["aeos"], ttl_seconds=ttl,
                                gov_approved=True)


@pytest.fixture
async def cluster(tmp_path):
    c = _Cluster(str(tmp_path / "keys"))
    await c.start()
    try:
        yield c
    finally:
        await c.stop()


# ── Governance ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_governance_request_verify_revoke(cluster):
    stub = governance_pb2_grpc.GovernanceServiceStub(cluster.channel)

    # Approve → get a signed token.
    resp = await stub.RequestApproval(governance_pb2.ApprovalRequest(
        request_id="r1", subject_id="task-1", subject_type="task",
        requester_id="scheduler", ttl_seconds=120))
    assert resp.decision == governance_pb2.APPROVAL_DECISION_APPROVED
    assert resp.governance_token, "no token minted for approved request"

    # Verify → valid.
    v = await stub.VerifyToken(governance_pb2.VerifyTokenRequest(
        governance_token=resp.governance_token, expected_audience=["aeos"]))
    assert v.valid is True
    assert v.subject_id == "task-1"

    # Tampered token → fail-closed (valid=False, no exception).
    bad = await stub.VerifyToken(governance_pb2.VerifyTokenRequest(
        governance_token=resp.governance_token + "x", expected_audience=["aeos"]))
    assert bad.valid is False
    assert bad.failure_reason

    # Audit log recorded the decision.
    audit = await stub.QueryAuditLog(governance_pb2.AuditQueryRequest(subject_id="task-1"))
    assert any(r.request_id == "r1" for r in audit.records)


@pytest.mark.asyncio
async def test_governance_denies_empty_subject(cluster):
    stub = governance_pb2_grpc.GovernanceServiceStub(cluster.channel)
    resp = await stub.RequestApproval(governance_pb2.ApprovalRequest(
        request_id="r2", subject_id="", requester_id="x"))
    assert resp.decision == governance_pb2.APPROVAL_DECISION_DENIED
    assert resp.governance_token == "", "denied request must not mint a token"


# ── Scheduler (fail-closed governance) ───────────────────────────────────────

@pytest.mark.asyncio
async def test_scheduler_requires_valid_token(cluster):
    stub = task_pb2_grpc.SchedulerServiceStub(cluster.channel)
    token = cluster.sign_task_token("task-ok")

    ok = await stub.ScheduleTask(task_pb2.ScheduleTaskRequest(
        task=task_pb2.Task(task_id="task-ok", assigned_worker_id="w1",
                           governance_token=token),
        require_governance=True))
    assert ok.task_id == "task-ok"
    assert ok.assigned_worker_id == "w1"

    # Status query over the wire.
    st = await stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="task-ok"))
    assert st.status == task_pb2.TASK_STATUS_ASSIGNED

    # Unsigned task addressed with require_governance → PERMISSION_DENIED.
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.ScheduleTask(task_pb2.ScheduleTaskRequest(
            task=task_pb2.Task(task_id="task-bad", assigned_worker_id="w1"),
            require_governance=True))
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED

    # And the rejected task never entered the registry.
    with pytest.raises(grpc.aio.AioRpcError) as ei2:
        await stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="task-bad"))
    assert ei2.value.code() == grpc.StatusCode.NOT_FOUND


@pytest.mark.asyncio
async def test_scheduler_cancel(cluster):
    stub = task_pb2_grpc.SchedulerServiceStub(cluster.channel)
    await stub.ScheduleTask(task_pb2.ScheduleTaskRequest(
        task=task_pb2.Task(task_id="c1", assigned_worker_id="w1")))
    c = await stub.CancelTask(task_pb2.CancelTaskRequest(task_id="c1"))
    assert c.cancelled is True
    st = await stub.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="c1"))
    assert st.status == task_pb2.TASK_STATUS_CANCELLED


# ── Worker fleet ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_register_heartbeat_deregister(cluster):
    stub = worker_pb2_grpc.WorkerServiceStub(cluster.channel)

    reg = await stub.RegisterWorker(worker_pb2.RegisterWorkerRequest(
        worker_id="w-a", node_address="127.0.0.1:9101",
        capabilities=worker_pb2.WorkerCapabilities(
            supported_task_types=["echo", "map"], max_concurrency=8)))
    assert reg.cluster_member_id == "w-a"
    assert reg.dispatch_concurrency_limit == 8
    assert await _wait(lambda: cluster.worker.pool.workers()
                       and cluster.worker.pool.workers()[0].node_id == "w-a")

    hb = await stub.SendHeartbeat(worker_pb2.WorkerHeartbeat(
        worker_id="w-a", status=worker_pb2.WORKER_STATUS_BUSY,
        active_task_count=3, cpu_utilization=0.5))
    assert hb.directive == worker_pb2.WORKER_DIRECTIVE_NONE
    snap = await cluster.worker.pool.get("w-a")
    assert snap.in_flight_tasks == 3

    # A scheduler-set drain directive rides back on the next heartbeat.
    cluster.worker.set_directive("w-a", worker_pb2.WORKER_DIRECTIVE_DRAIN)
    hb2 = await stub.SendHeartbeat(worker_pb2.WorkerHeartbeat(worker_id="w-a"))
    assert hb2.directive == worker_pb2.WORKER_DIRECTIVE_DRAIN

    dereg = await stub.DeregisterWorker(worker_pb2.DeregisterWorkerRequest(
        worker_id="w-a", reason="test done"))
    assert dereg.acknowledged is True
    assert await _wait(lambda: not cluster.worker.pool.workers())


# ── Observability ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_observability_submit_and_watch(cluster):
    stub = observability_pb2_grpc.ObservabilityServiceStub(cluster.channel)

    sub = await stub.SubmitSpans(observability_pb2.SubmitSpansRequest(spans=[
        observability_pb2.Span(trace_id="t1", span_id="s1", operation_name="op"),
        observability_pb2.Span(trace_id="", span_id="s2"),  # invalid → rejected
    ]))
    assert sub.accepted == 1 and sub.rejected == 1

    # Live event stream: open WatchEvents, then submit; expect to receive it.
    received: list[str] = []
    call = stub.WatchEvents(observability_pb2.WatchEventsRequest(
        event_types=["task.completed"]))

    async def _watcher():
        async for ev in call:
            received.append(ev.event_id)
            break

    task = asyncio.create_task(_watcher())
    await asyncio.sleep(0.1)  # let the stream establish
    await stub.SubmitEvents(observability_pb2.SubmitEventsRequest(events=[
        observability_pb2.StructuredEvent(
            event_id="e1", event_type="task.completed", source="worker"),
    ]))
    await asyncio.wait_for(task, timeout=5.0)
    assert received == ["e1"]
    call.cancel()  # close the server-side stream so teardown doesn't dangle


# ── Federation (fail-closed session gate) ────────────────────────────────────

@pytest.mark.asyncio
async def test_federation_handshake_and_dispatch(cluster):
    stub = federation_pb2_grpc.FederationServiceStub(cluster.channel)

    hs = await stub.Handshake(federation_pb2.FederationHandshakeRequest(
        initiator=federation_pb2.ClusterIdentity(
            cluster_id="cluster-b", display_name="B"),
        supported_algorithms=["ES256"]))
    assert hs.session_token, "no federation session token issued"
    assert "ES256" in hs.accepted_algorithms

    # With a valid session token → task is dispatched into the local scheduler.
    disp = await stub.DispatchFederatedTask(federation_pb2.FederatedTaskRequest(
        task=task_pb2.Task(task_id="fed-1", assigned_worker_id="w9"),
        originating_cluster_id="cluster-b",
        federation_session_token=hs.session_token))
    assert disp.remote_task_id == "remote-fed-1"

    sched = task_pb2_grpc.SchedulerServiceStub(cluster.channel)
    st = await sched.GetTaskStatus(task_pb2.GetTaskStatusRequest(task_id="fed-1"))
    assert st.worker_id == "w9"

    # Without a session token → PERMISSION_DENIED, nothing dispatched.
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.DispatchFederatedTask(federation_pb2.FederatedTaskRequest(
            task=task_pb2.Task(task_id="fed-2"),
            originating_cluster_id="cluster-b"))
    assert ei.value.code() == grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_federation_rejects_algorithm_mismatch(cluster):
    stub = federation_pb2_grpc.FederationServiceStub(cluster.channel)
    with pytest.raises(grpc.aio.AioRpcError) as ei:
        await stub.Handshake(federation_pb2.FederationHandshakeRequest(
            initiator=federation_pb2.ClusterIdentity(cluster_id="cluster-c"),
            supported_algorithms=["HS256"]))
    assert ei.value.code() == grpc.StatusCode.FAILED_PRECONDITION
