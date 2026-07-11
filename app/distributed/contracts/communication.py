"""
Communication contracts — service discovery, RPC channels.

ServiceDiscovery: register/deregister/resolve service endpoints.
RpcChannel: typed async RPC with streaming support.

Contract: AC-COMM-001
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncIterator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ServiceEndpoint:
    node_id: str
    service_name: str
    host: str
    port: int
    metadata: dict[str, str] = field(default_factory=dict)
    registered_at: str = field(default_factory=_now_iso)


class ServiceDiscovery(ABC):
    """
    Service registry and discovery.

    Contract: AC-COMM-001
    """

    @abstractmethod
    async def register(self, endpoint: ServiceEndpoint) -> None:
        """Register a service endpoint."""

    @abstractmethod
    async def deregister(self, node_id: str, service_name: str) -> None:
        """Remove a specific service registration for a node."""

    @abstractmethod
    async def deregister_node(self, node_id: str) -> None:
        """Remove all service registrations for a node."""

    @abstractmethod
    async def resolve(self, service_name: str) -> list[ServiceEndpoint]:
        """Return all healthy endpoints for a service."""

    @abstractmethod
    async def resolve_one(
        self, service_name: str, *, node_id: str | None = None
    ) -> ServiceEndpoint | None:
        """Return one endpoint for a service, optionally pinned to a node."""

    @abstractmethod
    async def list_services(self) -> list[str]:
        """Return all registered service names."""

    @abstractmethod
    def watch(self, service_name: str) -> AsyncIterator[list[ServiceEndpoint]]:
        """Yield updated endpoint lists whenever the service registry changes."""


class RpcChannel(ABC):
    """
    Typed async RPC channel.

    Implementations: InMemoryRpcChannel (test), GrpcChannel (production).
    """

    @abstractmethod
    async def connect(self, *, timeout_seconds: float = 5.0) -> None:
        """Establish the channel connection."""

    @abstractmethod
    async def close(self) -> None:
        """Close the channel and release resources."""

    @abstractmethod
    async def make_call(
        self,
        method: str,
        request: dict,
        *,
        timeout_seconds: float = 30.0,
        metadata: dict[str, str] | None = None,
    ) -> dict:
        """Execute a unary RPC call and return the response."""

    @abstractmethod
    async def make_streaming_call(
        self,
        method: str,
        request: dict,
        *,
        timeout_seconds: float = 60.0,
        metadata: dict[str, str] | None = None,
    ) -> AsyncIterator[dict]:
        """Execute a server-streaming RPC call."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Return True if the remote endpoint is reachable."""
