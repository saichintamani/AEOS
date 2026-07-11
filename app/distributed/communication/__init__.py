"""Service discovery and RPC channel implementations."""

from app.distributed.communication.discovery import InMemoryServiceDiscovery
from app.distributed.communication.channel import InMemoryRpcChannel, GrpcChannel

__all__ = ["InMemoryServiceDiscovery", "InMemoryRpcChannel", "GrpcChannel"]
