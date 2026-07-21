"""
Integration — signed task dispatched over the gRPC event bus drives a
production-configured WorkerRuntime end-to-end.

Two nodes, two real GrpcEventBusTransport servers on ephemeral localhost ports:
  - "scheduler" node: publishes a TASK_ACCEPTED envelope (via the normal
    DefaultEventPublisher → router → serializer → transport chain).
  - "worker" node: built by the production bootstrap in environment=production,
    so require_signed_tokens is forced True and a real TokenVerifier is wired.

Proves over the wire:
  - a signed, governance-approved task is delivered cross-transport and executes;
  - an unsigned task is rejected fail-closed by governance and never runs;
  - the worker's TASK_COMPLETED result is published back and observed by the
    scheduler node.

This is the same-process (real-socket) sibling of the cross-process testbed in
test_grpc_cluster.py.

Phase: 13 Sprint 2
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")
pytest.importorskip("cryptography", reason="cryptography not installed")

from app.core.config import AEOSSettings
from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.transport.grpc_bus import GrpcEventBusTransport
from app.distributed.worker.bootstrap import build_worker_runtime
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner


async def _wait(pred, timeout=4.0, interval=0.02):
    for _ in range(int(timeout / interval)):
        if pred():
            return True
        await asyncio.sleep(interval)
    return pred()


def _worker_settings(tmp_path) -> AEOSSettings:
    return AEOSSettings(
        environment="production",
        token_keys_dir=str(tmp_path / "keys"),
        token_issuer="aeos",
        token_algorithm="ES256",
    )


def _accepted_envelope(worker_node, *, token_id=None, raw_token=None, task_id="t-1"):
    payload = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "step_id": "step-1",
        "task_type": "echo",
        "task_payload": {},
        "priority": "normal",
        "lease_key": "exec:wf-1:step-1",
        "fencing_token": 1,
        "assigned_worker_id": worker_node,
        "attempt": 0,
        "max_attempts": 3,
    }
    if token_id is not None:
        payload["token_id"] = token_id
    if raw_token is not None:
        payload["raw_token"] = raw_token
    return EventEnvelope(
        event_type=DistributedEventType.TASK_ACCEPTED,
        payload=payload,
        source_node_id="scheduler",
        workflow_id="wf-1",
        task_id=task_id,
    )


class _Cluster:
    """Two peered gRPC transports + a scheduler publisher/consumer."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.sched_tp = GrpcEventBusTransport("scheduler", port=0)
        self.worker_tp = GrpcEventBusTransport("worker-1", port=0)
        self.serializer = JsonEventSerializer()

    async def start(self):
        await self.sched_tp.start()
        await self.worker_tp.start()
        self.sched_tp.add_peer(self.worker_tp.address)
        self.worker_tp.add_peer(self.sched_tp.address)

        # Scheduler-side publisher + a consumer to observe results.
        self.sched_pub = DefaultEventPublisher(
            clock=MonotonicClock(), router=DefaultEventRouter(),
            serializer=self.serializer, transport=self.sched_tp,
            source_node_id="scheduler",
        )
        self.sched_con = DefaultEventConsumer(self.sched_tp, self.serializer, node_id="scheduler")
        self.completed: list[str] = []

        async def on_completed(env: EventEnvelope):
            # TASK_ACCEPTED and TASK_COMPLETED share the aeos.events.execution
            # topic; the consumer dispatches by topic, so filter by type here.
            if env.event_type != DistributedEventType.TASK_COMPLETED:
                return
            self.completed.append(env.payload.get("task_id", ""))

        await self.sched_con.subscribe([DistributedEventType.TASK_COMPLETED], on_completed, "scheduler")
        await self.sched_con.start()

        # Worker built by the PRODUCTION bootstrap over the worker transport.
        self.worker = build_worker_runtime(
            NodeIdentity(node_id="worker-1", host="127.0.0.1", port=self.worker_tp.bound_port),
            settings=_worker_settings(self.tmp_path),
            transport=self.worker_tp,
        )
        self.ran: list[str] = []

        async def handler(ctx, _):
            self.ran.append(ctx.task_id)
            return {"ok": True}

        self.worker.register_handler("echo", handler)
        await self.worker.start()

    async def stop(self):
        await self.worker.stop()
        await self.sched_con.stop()
        await self.sched_tp.stop()
        await self.worker_tp.stop()

    def sign(self, subject="task-signed"):
        store = KeyStore(keys_dir=_worker_settings(self.tmp_path).token_keys_dir,
                         algorithm=KeyAlgorithm.ES256)
        store.initialize()
        return TokenSigner(store, issuer="aeos").sign(subject, audience=["aeos"], ttl_seconds=60)


@pytest.mark.asyncio
async def test_signed_task_executes_over_grpc(tmp_path):
    c = _Cluster(tmp_path)
    await c.start()
    try:
        token = c.sign()
        await c.sched_pub.publish(
            _accepted_envelope("worker-1", token_id="task-signed", raw_token=token, task_id="t-signed")
        )
        assert await _wait(lambda: c.ran == ["t-signed"]), f"ran={c.ran}"
        assert await _wait(lambda: "t-signed" in c.completed), f"completed={c.completed}"
    finally:
        await c.stop()


@pytest.mark.asyncio
async def test_unsigned_task_rejected_over_grpc(tmp_path):
    c = _Cluster(tmp_path)
    await c.start()
    try:
        # No token → production governance rejects fail-closed.
        await c.sched_pub.publish(_accepted_envelope("worker-1", task_id="t-unsigned"))
        assert await _wait(lambda: c.worker.metrics.failed_tasks == 1), \
            f"failed={c.worker.metrics.failed_tasks}"
        assert c.ran == []
    finally:
        await c.stop()
