"""
Unit tests — InMemoryServiceDiscovery (watch(), resolve_one()), InMemoryRpcChannel.

Contract: AC-COMM-001
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.contracts.communication import ServiceEndpoint
from app.distributed.communication.discovery import InMemoryServiceDiscovery
from app.distributed.communication.channel import InMemoryRpcChannel


def _ep(node: str, svc: str = "executor") -> ServiceEndpoint:
    return ServiceEndpoint(node_id=node, service_name=svc, host="127.0.0.1", port=9000)


class TestInMemoryServiceDiscovery:

    @pytest.mark.asyncio
    async def test_register_and_resolve(self):
        sd = InMemoryServiceDiscovery()
        await sd.register(_ep("n1"))
        endpoints = await sd.resolve("executor")
        assert len(endpoints) == 1
        assert endpoints[0].node_id == "n1"

    @pytest.mark.asyncio
    async def test_deregister(self):
        sd = InMemoryServiceDiscovery()
        await sd.register(_ep("n1"))
        await sd.deregister("n1", "executor")
        assert await sd.resolve("executor") == []

    @pytest.mark.asyncio
    async def test_deregister_node_removes_all(self):
        sd = InMemoryServiceDiscovery()
        await sd.register(_ep("n1", "svc-a"))
        await sd.register(_ep("n1", "svc-b"))
        await sd.deregister_node("n1")
        assert await sd.resolve("svc-a") == []
        assert await sd.resolve("svc-b") == []

    @pytest.mark.asyncio
    async def test_resolve_one_by_node(self):
        sd = InMemoryServiceDiscovery()
        await sd.register(_ep("n1"))
        await sd.register(_ep("n2"))
        ep = await sd.resolve_one("executor", node_id="n2")
        assert ep is not None
        assert ep.node_id == "n2"

    @pytest.mark.asyncio
    async def test_list_services(self):
        sd = InMemoryServiceDiscovery()
        await sd.register(_ep("n1", "svc-a"))
        await sd.register(_ep("n1", "svc-b"))
        services = await sd.list_services()
        assert "svc-a" in services
        assert "svc-b" in services

    @pytest.mark.asyncio
    async def test_watch_yields_updates(self):
        sd = InMemoryServiceDiscovery()

        updates = []

        async def consume_one():
            async for endpoints in sd.watch("my-svc"):
                updates.append(endpoints)
                break  # stop after first update

        task = asyncio.create_task(consume_one())
        await asyncio.sleep(0.01)
        await sd.register(_ep("n1", "my-svc"))
        await asyncio.wait_for(task, timeout=1.0)
        # We should have received at least 1 update (the initial empty + the register)
        assert len(updates) >= 1


class TestInMemoryRpcChannel:

    @pytest.mark.asyncio
    async def test_make_call_dispatches_handler(self):
        ch = InMemoryRpcChannel("n1")

        async def my_handler(req: dict) -> dict:
            return {"echo": req.get("msg")}

        ch.register_handler("echo", my_handler)
        await ch.connect()
        result = await ch.make_call("echo", {"msg": "hello"})
        assert result["echo"] == "hello"

    @pytest.mark.asyncio
    async def test_unknown_method_raises(self):
        ch = InMemoryRpcChannel()
        await ch.connect()
        with pytest.raises(NotImplementedError):
            await ch.make_call("unknown", {})

    @pytest.mark.asyncio
    async def test_health_check(self):
        ch = InMemoryRpcChannel()
        assert not await ch.health_check()
        await ch.connect()
        assert await ch.health_check()
        await ch.close()
        assert not await ch.health_check()
