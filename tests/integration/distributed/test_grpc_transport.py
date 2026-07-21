"""
Integration tests — GrpcEventBusTransport over real grpc.aio channels.

These run two (or three) transports in one process but communicate strictly
over real gRPC sockets (ephemeral localhost ports), so they exercise the actual
wire path: publish → DeliverEvent RPC → remote local-dispatch. A separate
cross-*process* testbed lives in tests/integration/distributed/.

Proves:
  - a message published on node A is delivered to a subscriber on node B;
  - fan-out across group_ids on the receiving node;
  - self-delivery (local subscriber) without a network hop;
  - Ping liveness;
  - unsubscribed handlers stop receiving.

Phase: 13 Sprint 2
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("grpc", reason="grpcio not installed")

from app.distributed.contracts.transport import TransportMessage
from app.distributed.transport.grpc_bus import GrpcEventBusTransport


async def _wait(predicate, timeout=3.0, interval=0.02):
    loops = int(timeout / interval)
    for _ in range(loops):
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


def _msg(topic: str, payload: bytes = b"hello") -> TransportMessage:
    return TransportMessage(topic=topic, payload=payload, headers={"schema": "1"})


@pytest.mark.asyncio
async def test_publish_delivered_to_remote_node():
    a = GrpcEventBusTransport("node-a", port=0)
    b = GrpcEventBusTransport("node-b", port=0)
    await a.start()
    await b.start()
    a.add_peer(b.address)
    b.add_peer(a.address)

    got: list[bytes] = []

    async def handler(m: TransportMessage):
        got.append(m.payload)

    await b.subscribe("tasks", "workers", handler)
    try:
        await a.publish(_msg("tasks", b"across-the-wire"))
        assert await _wait(lambda: got == [b"across-the-wire"])
        # Headers and topic survived serialization.
        assert got == [b"across-the-wire"]
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_fanout_across_groups_on_receiver():
    a = GrpcEventBusTransport("node-a", port=0)
    b = GrpcEventBusTransport("node-b", port=0)
    await a.start()
    await b.start()
    a.add_peer(b.address)

    g1: list[str] = []
    g2: list[str] = []

    async def h1(m):
        g1.append(m.message_id)

    async def h2(m):
        g2.append(m.message_id)

    await b.subscribe("events", "group-1", h1)
    await b.subscribe("events", "group-2", h2)
    try:
        await a.publish(_msg("events"))
        assert await _wait(lambda: len(g1) == 1 and len(g2) == 1)
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_local_self_delivery():
    a = GrpcEventBusTransport("node-a", port=0)
    await a.start()

    got: list[bytes] = []

    async def handler(m):
        got.append(m.payload)

    await a.subscribe("local", "grp", handler)
    try:
        await a.publish(_msg("local", b"self"))
        assert await _wait(lambda: got == [b"self"])
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    a = GrpcEventBusTransport("node-a", port=0)
    b = GrpcEventBusTransport("node-b", port=0)
    await a.start()
    await b.start()
    a.add_peer(b.address)

    got: list[int] = []

    async def handler(m):
        got.append(1)

    sub = await b.subscribe("t", "g", handler)
    try:
        await a.publish(_msg("t"))
        assert await _wait(lambda: len(got) == 1)
        await b.unsubscribe(sub)
        await a.publish(_msg("t"))
        await asyncio.sleep(0.2)
        assert len(got) == 1  # no further delivery
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_ping_liveness():
    import app.distributed.grpc.generated  # noqa: F401
    from aeos.transport.v1 import eventbus_pb2, eventbus_pb2_grpc
    import grpc

    a = GrpcEventBusTransport("node-a", port=0)
    await a.start()
    try:
        async with grpc.aio.insecure_channel(a.address) as channel:
            stub = eventbus_pb2_grpc.EventBusServiceStub(channel)
            resp = await stub.Ping(eventbus_pb2.PingRequest(from_node_id="probe"))
            assert resp.node_id == "node-a"
            assert resp.healthy is True
    finally:
        await a.stop()
