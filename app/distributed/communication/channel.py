"""
RPC channel implementations.

InMemoryRpcChannel: handler-registry based, for unit tests.
GrpcChannel: placeholder that raises NotImplementedError (production impl external).

Contract: AC-COMM-001
"""

from __future__ import annotations

from typing import AsyncIterator, Callable, Coroutine, Any

from app.distributed.contracts.communication import RpcChannel


class InMemoryRpcChannel(RpcChannel):
    """
    In-process RPC channel backed by a method handler registry.

    Register handlers with register_handler(method, coro_fn).
    make_call dispatches synchronously within the same process.
    """

    def __init__(self, node_id: str = "") -> None:
        self._node_id = node_id
        self._handlers: dict[str, Callable[[dict], Coroutine[Any, Any, dict]]] = {}
        self._connected = False

    def register_handler(
        self,
        method: str,
        handler: Callable[[dict], Coroutine[Any, Any, dict]],
    ) -> None:
        self._handlers[method] = handler

    async def connect(self, *, timeout_seconds: float = 5.0) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def make_call(
        self,
        method: str,
        request: dict,
        *,
        timeout_seconds: float = 30.0,
        metadata: dict[str, str] | None = None,
    ) -> dict:
        handler = self._handlers.get(method)
        if handler is None:
            raise NotImplementedError(f"No handler registered for method {method!r}")
        return await handler(request)

    async def make_streaming_call(
        self,
        method: str,
        request: dict,
        *,
        timeout_seconds: float = 60.0,
        metadata: dict[str, str] | None = None,
    ) -> AsyncIterator[dict]:
        raise NotImplementedError("Streaming not implemented in InMemoryRpcChannel")
        # unreachable; needed to satisfy return type
        yield {}  # type: ignore[misc]

    async def health_check(self) -> bool:
        return self._connected


class GrpcChannel(RpcChannel):
    """Production gRPC channel. Implementation deferred to external grpc package."""

    def __init__(self, address: str) -> None:
        self._address = address

    async def connect(self, *, timeout_seconds: float = 5.0) -> None:
        raise NotImplementedError("GrpcChannel requires grpcio — not yet wired")

    async def close(self) -> None:
        raise NotImplementedError("GrpcChannel requires grpcio — not yet wired")

    async def make_call(self, method: str, request: dict, **kwargs) -> dict:
        raise NotImplementedError("GrpcChannel requires grpcio — not yet wired")

    async def make_streaming_call(self, method: str, request: dict, **kwargs) -> AsyncIterator[dict]:
        raise NotImplementedError("GrpcChannel requires grpcio — not yet wired")
        yield {}  # type: ignore[misc]

    async def health_check(self) -> bool:
        raise NotImplementedError("GrpcChannel requires grpcio — not yet wired")
