"""
In-memory service discovery with asyncio.Queue-based watch() generator.

Suitable for single-process testing. Production: ConsulServiceDiscovery or
etcd-backed implementation.

Contract: AC-COMM-001
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import AsyncIterator

from app.distributed.contracts.communication import ServiceDiscovery, ServiceEndpoint


class InMemoryServiceDiscovery(ServiceDiscovery):
    """
    Dict-backed service registry.

    watch() returns an async generator that yields the current endpoint list
    whenever the registry changes for the watched service.
    """

    def __init__(self) -> None:
        # service_name → node_id → ServiceEndpoint
        self._registry: dict[str, dict[str, ServiceEndpoint]] = defaultdict(dict)
        self._lock = asyncio.Lock()
        # service_name → list of notification queues (one per watch() caller)
        self._watchers: dict[str, list[asyncio.Queue]] = defaultdict(list)

    async def register(self, endpoint: ServiceEndpoint) -> None:
        async with self._lock:
            self._registry[endpoint.service_name][endpoint.node_id] = endpoint
            await self._notify(endpoint.service_name)

    async def deregister(self, node_id: str, service_name: str) -> None:
        async with self._lock:
            self._registry[service_name].pop(node_id, None)
            await self._notify(service_name)

    async def deregister_node(self, node_id: str) -> None:
        async with self._lock:
            changed: set[str] = set()
            for svc, nodes in self._registry.items():
                if node_id in nodes:
                    del nodes[node_id]
                    changed.add(svc)
            for svc in changed:
                await self._notify(svc)

    async def resolve(self, service_name: str) -> list[ServiceEndpoint]:
        async with self._lock:
            return list(self._registry[service_name].values())

    async def resolve_one(
        self, service_name: str, *, node_id: str | None = None
    ) -> ServiceEndpoint | None:
        async with self._lock:
            if node_id:
                return self._registry[service_name].get(node_id)
            nodes = list(self._registry[service_name].values())
            return nodes[0] if nodes else None

    async def list_services(self) -> list[str]:
        async with self._lock:
            return [s for s, nodes in self._registry.items() if nodes]

    async def _notify(self, service_name: str) -> None:
        endpoints = list(self._registry[service_name].values())
        for q in self._watchers[service_name]:
            await q.put(endpoints)

    async def watch(self, service_name: str) -> AsyncIterator[list[ServiceEndpoint]]:  # type: ignore[override]
        q: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._watchers[service_name].append(q)
            current = list(self._registry[service_name].values())
        yield current
        try:
            while True:
                endpoints = await q.get()
                yield endpoints
        finally:
            async with self._lock:
                self._watchers[service_name].remove(q)
