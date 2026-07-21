"""
gRPC event-bus transport — the wire realization of MessageTransport.

Each AEOS node runs a grpc.aio EventBusService server and holds client
channels to its peers. ``publish()`` dispatches the message to local
subscribers directly and pushes it to every remote peer via a DeliverEvent
RPC; each receiving node then fans the message out to *its* local subscriber
groups. The domain event stack (publisher → router → consumer → worker
runtime, plus GovernanceClient) rides on top of this unchanged — so making the
transport real over gRPC makes task dispatch, governance enforcement, and
checkpoint traffic flow across physically separate processes.

Delivery semantics (documented, and matched to how AEOS uses events):
  - Fan-out across group_ids is preserved on every node.
  - Competing-consumer (round-robin within a group_id) is **node-local**: each
    node delivers a message to exactly one handler per local group. Across N
    nodes a group may therefore receive up to N copies. AEOS relies on task
    *addressing* (workers filter TASK_ACCEPTED by assigned_worker_id) and on
    governance events being intentionally broadcast, so broadcast + node-local
    round-robin is correct for the platform. A subscription-aware router that
    delivers exactly once per group cluster-wide is a later optimization.

Loop control: DeliverEvent only performs local dispatch; it never re-publishes.
publish() sends to each remote peer exactly once. origin_node_id is stamped for
audit and echo suppression.

Contract: AC-TRANS-001 (same ABC as InMemoryTransport / KafkaTransport)
Phase: 13 Sprint 2 (real distributed runtime)
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Any

from app.distributed.contracts.transport import MessageHandler, MessageTransport, TransportMessage

# Import the generated-stub package once to run its sys.path shim, then pull the
# aeos.* rooted modules it exposes.
import app.distributed.grpc.generated  # noqa: F401  (side effect: sys.path shim)
from aeos.transport.v1 import eventbus_pb2, eventbus_pb2_grpc

logger = logging.getLogger(__name__)


def _require_grpc_aio() -> Any:
    try:
        import grpc
        import grpc.aio  # noqa: F401
        return grpc
    except ImportError as exc:  # pragma: no cover - env guard
        raise ImportError(
            "grpcio is required for GrpcEventBusTransport. Install: pip install grpcio"
        ) from exc


def _to_bus_message(msg: TransportMessage, origin_node_id: str) -> "eventbus_pb2.BusMessage":
    return eventbus_pb2.BusMessage(
        topic=msg.topic,
        payload=msg.payload,
        message_id=msg.message_id,
        produced_at=msg.produced_at,
        headers=dict(msg.headers or {}),
        partition=msg.partition or 0,
        offset=msg.offset or 0,
        key=msg.key or b"",
        trace_id=msg.trace_id or "",
        span_id=msg.span_id or "",
        origin_node_id=origin_node_id,
    )


def _from_bus_message(bm: "eventbus_pb2.BusMessage") -> TransportMessage:
    return TransportMessage(
        topic=bm.topic,
        payload=bm.payload,
        message_id=bm.message_id or str(uuid.uuid4()),
        produced_at=bm.produced_at,
        headers=dict(bm.headers),
        partition=bm.partition or None,
        offset=bm.offset or None,
        key=bm.key or None,
        trace_id=bm.trace_id or None,
        span_id=bm.span_id or None,
    )


class _EventBusServicer(eventbus_pb2_grpc.EventBusServiceServicer):
    """Receives DeliverEvent RPCs from peers and hands them to the transport."""

    def __init__(self, transport: "GrpcEventBusTransport") -> None:
        self._transport = transport

    async def DeliverEvent(self, request, context):  # noqa: N802 (gRPC naming)
        if not self._transport.is_running:
            return eventbus_pb2.DeliverAck(accepted=False, error="node not running")
        msg = _from_bus_message(request)
        group_count = await self._transport._dispatch_local(msg)
        return eventbus_pb2.DeliverAck(accepted=True, local_group_count=group_count)

    async def Ping(self, request, context):  # noqa: N802
        return eventbus_pb2.PingResponse(
            node_id=self._transport.node_id, healthy=self._transport.is_running
        )


class GrpcEventBusTransport(MessageTransport):
    """
    Cross-process MessageTransport backed by grpc.aio.

    Args:
        node_id:  Stable identifier for this node (stamped as origin).
        host:     Bind host for this node's EventBusService server.
        port:     Bind port; pass 0 for an ephemeral port (read back via
                  ``bound_port`` after start() — handy for tests).
        peers:    "host:port" targets of the OTHER nodes in the cluster
                  (exclude self). May be updated later via add_peer().
        rpc_timeout_seconds: Per-DeliverEvent deadline.
    """

    def __init__(
        self,
        node_id: str,
        host: str = "127.0.0.1",
        port: int = 0,
        peers: list[str] | None = None,
        *,
        rpc_timeout_seconds: float = 5.0,
    ) -> None:
        self._grpc = _require_grpc_aio()
        self._node_id = node_id
        self._host = host
        self._port = port
        self._bound_port: int | None = None
        self._peers: set[str] = set(peers or [])
        self._rpc_timeout = rpc_timeout_seconds

        # Local subscription registry — identical shape to InMemoryTransport.
        self._subs: dict[str, dict[str, list[MessageHandler]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._sub_map: dict[str, tuple[str, str, MessageHandler]] = {}
        self._rr_index: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._lock = asyncio.Lock()

        self._server: Any = None
        self._channels: dict[str, Any] = {}
        self._stubs: dict[str, Any] = {}
        self._running = False
        self._published: dict[str, int] = defaultdict(int)

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bound_port(self) -> int | None:
        """Actual listening port (resolved even when constructed with port=0)."""
        return self._bound_port

    @property
    def address(self) -> str:
        return f"{self._host}:{self._bound_port if self._bound_port else self._port}"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = self._grpc.aio.server()
        eventbus_pb2_grpc.add_EventBusServiceServicer_to_server(
            _EventBusServicer(self), self._server
        )
        bind_target = f"{self._host}:{self._port}"
        self._bound_port = self._server.add_insecure_port(bind_target)
        await self._server.start()
        for peer in self._peers:
            self._ensure_stub(peer)
        self._running = True
        logger.info("GrpcEventBusTransport %s listening on %s peers=%s",
                    self._node_id, self.address, sorted(self._peers))

    async def stop(self) -> None:
        self._running = False
        for ch in self._channels.values():
            try:
                await ch.close()
            except Exception:  # pragma: no cover
                pass
        self._channels.clear()
        self._stubs.clear()
        if self._server is not None:
            await self._server.stop(grace=1.0)
            self._server = None

    # ── Peer management ───────────────────────────────────────────────────────

    def add_peer(self, target: str) -> None:
        """Register another node's EventBusService address (host:port)."""
        if target and target != self.address:
            self._peers.add(target)
            if self._running:
                self._ensure_stub(target)

    def _ensure_stub(self, target: str) -> Any:
        stub = self._stubs.get(target)
        if stub is None:
            channel = self._grpc.aio.insecure_channel(target)
            self._channels[target] = channel
            stub = eventbus_pb2_grpc.EventBusServiceStub(channel)
            self._stubs[target] = stub
        return stub

    # ── Publish ───────────────────────────────────────────────────────────────

    async def publish(self, message: TransportMessage, *, wait_for_ack: bool = True) -> None:
        self._published[message.topic] += 1
        # Local delivery first (no network hop to self).
        await self._dispatch_local(message)
        if not self._peers:
            return
        bus_msg = _to_bus_message(message, self._node_id)
        coros = [self._send_to_peer(peer, bus_msg) for peer in list(self._peers)]
        results = await asyncio.gather(*coros, return_exceptions=True)
        if wait_for_ack:
            for peer, res in zip(list(self._peers), results):
                if isinstance(res, Exception):
                    logger.warning("DeliverEvent to %s failed: %s", peer, res)

    async def _send_to_peer(self, target: str, bus_msg: "eventbus_pb2.BusMessage") -> None:
        stub = self._ensure_stub(target)
        await stub.DeliverEvent(bus_msg, timeout=self._rpc_timeout)

    # ── Local dispatch (mirrors InMemoryTransport semantics) ──────────────────

    async def _dispatch_local(self, message: TransportMessage) -> int:
        """Fan out to local groups, round-robin one handler per group. Returns
        the number of local groups the message was delivered to."""
        async with self._lock:
            topic_subs = self._subs.get(message.topic, {})
            delivered = 0
            for group_id, handlers in list(topic_subs.items()):
                if not handlers:
                    continue
                idx = self._rr_index[message.topic][group_id] % len(handlers)
                self._rr_index[message.topic][group_id] = idx + 1
                handler = handlers[idx]
                asyncio.create_task(self._safe_call(handler, message))
                delivered += 1
            return delivered

    @staticmethod
    async def _safe_call(handler: MessageHandler, msg: TransportMessage) -> None:
        try:
            await handler(msg)
        except Exception:  # pragma: no cover - handler robustness
            logger.exception("GrpcEventBusTransport handler error")

    # ── Subscription registry ─────────────────────────────────────────────────

    async def subscribe(
        self,
        topics: list[str] | str,
        group_id: str,
        handler: MessageHandler,
        *,
        auto_commit: bool = False,
    ) -> str:
        if isinstance(topics, str):
            topics = [topics]
        sub_id = str(uuid.uuid4())
        async with self._lock:
            for topic in topics:
                self._subs[topic][group_id].append(handler)
                self._sub_map[sub_id] = (topic, group_id, handler)
        return sub_id

    async def unsubscribe(self, subscription_id: str) -> None:
        async with self._lock:
            entry = self._sub_map.pop(subscription_id, None)
            if entry:
                topic, group_id, handler = entry
                handlers = self._subs.get(topic, {}).get(group_id, [])
                if handler in handlers:
                    handlers.remove(handler)

    async def commit(self, subscription_id: str, message: TransportMessage) -> None:
        pass  # at-least-once handled by the consumer layer; delivery ack is per-RPC

    async def health_check(self) -> bool:
        return self._running

    def published_count(self, topic: str) -> int:
        return self._published.get(topic, 0)
