"""
DomainServiceServer — one grpc.aio server hosting any subset of the five AEOS
domain servicers on a single port.

Register servicers before start(); each ``add_*`` call wires the servicer into
the server via its generated ``add_<Service>Servicer_to_server`` binder. start()
binds the port (ephemeral with port=0, read back via ``address``) and serves;
stop() drains gracefully. Lifecycle intentionally mirrors
GrpcEventBusTransport.start/stop so the two compose in the same process.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import logging
from typing import Any

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2_grpc as scheduler_grpc
from aeos.core.v1 import worker_pb2_grpc as worker_grpc
from aeos.federation.v1 import federation_pb2_grpc as federation_grpc
from aeos.governance.v1 import governance_pb2_grpc as governance_grpc
from aeos.observability.v1 import observability_pb2_grpc as observability_grpc

logger = logging.getLogger(__name__)


def _require_grpc() -> Any:
    try:
        import grpc
        import grpc.aio  # noqa: F401
        return grpc
    except ImportError as exc:  # pragma: no cover - env guard
        raise ImportError(
            "grpcio is required for DomainServiceServer. Install: pip install grpcio"
        ) from exc


class DomainServiceServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._grpc = _require_grpc()
        self._host = host
        self._port = port
        self._bound_port: int | None = None
        self._server: Any = None
        self._running = False
        # Deferred binders: (add_fn, servicer) applied at start().
        self._pending: list[tuple[Any, Any]] = []

    # ── registration ──────────────────────────────────────────────────────────
    def add_governance(self, servicer) -> "DomainServiceServer":
        self._pending.append(
            (governance_grpc.add_GovernanceServiceServicer_to_server, servicer))
        return self

    def add_scheduler(self, servicer) -> "DomainServiceServer":
        self._pending.append(
            (scheduler_grpc.add_SchedulerServiceServicer_to_server, servicer))
        return self

    def add_worker(self, servicer) -> "DomainServiceServer":
        self._pending.append(
            (worker_grpc.add_WorkerServiceServicer_to_server, servicer))
        return self

    def add_observability(self, servicer) -> "DomainServiceServer":
        self._pending.append(
            (observability_grpc.add_ObservabilityServiceServicer_to_server, servicer))
        return self

    def add_federation(self, servicer) -> "DomainServiceServer":
        self._pending.append(
            (federation_grpc.add_FederationServiceServicer_to_server, servicer))
        return self

    # ── lifecycle ─────────────────────────────────────────────────────────────
    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def bound_port(self) -> int | None:
        return self._bound_port

    @property
    def address(self) -> str:
        return f"{self._host}:{self._bound_port if self._bound_port else self._port}"

    async def start(self) -> None:
        if not self._pending:
            raise RuntimeError("DomainServiceServer.start() with no servicers registered")
        self._server = self._grpc.aio.server()
        for add_fn, servicer in self._pending:
            add_fn(servicer, self._server)
        self._bound_port = self._server.add_insecure_port(f"{self._host}:{self._port}")
        await self._server.start()
        self._running = True
        logger.info("DomainServiceServer listening on %s (%d services)",
                    self.address, len(self._pending))

    async def stop(self, grace: float = 1.0) -> None:
        self._running = False
        if self._server is not None:
            await self._server.stop(grace=grace)
            self._server = None
