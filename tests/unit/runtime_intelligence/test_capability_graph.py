"""Unit tests — CapabilityGraph."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.capability_graph import CapabilityGraph
from app.runtime_intelligence.contracts import CapabilityProfile


def _profile(
    worker_id: str,
    skills: frozenset[str] = frozenset(),
    models: list[str] | None = None,
    region: str = "us-east-1",
    gpu: bool = False,
    memory_gb: float = 8.0,
    trust_score: float = 0.9,
    health_score: float = 1.0,
) -> CapabilityProfile:
    return CapabilityProfile(
        worker_id=worker_id,
        skills=skills,
        supported_models=models or [],
        region=region,
        gpu_available=gpu,
        memory_gb=memory_gb,
        trust_score=trust_score,
        health_score=health_score,
    )


class TestCapabilityGraph:

    @pytest.mark.asyncio
    async def test_upsert_and_get(self):
        g = CapabilityGraph()
        p = _profile("w1")
        await g.upsert(p)
        result = await g.get("w1")
        assert result is not None
        assert result.worker_id == "w1"

    @pytest.mark.asyncio
    async def test_upsert_overwrites(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", memory_gb=8.0))
        await g.upsert(_profile("w1", memory_gb=32.0))
        result = await g.get("w1")
        assert result.memory_gb == 32.0

    @pytest.mark.asyncio
    async def test_remove(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1"))
        await g.remove("w1")
        assert await g.get("w1") is None

    @pytest.mark.asyncio
    async def test_skill_index(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", skills=frozenset({"nlp", "rag"})))
        await g.upsert(_profile("w2", skills=frozenset({"rag"})))
        await g.upsert(_profile("w3", skills=frozenset({"vision"})))
        results = await g.by_skill("rag")
        ids = {p.worker_id for p in results}
        assert ids == {"w1", "w2"}

    @pytest.mark.asyncio
    async def test_model_index(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", models=["gpt-4", "llama-3"]))
        await g.upsert(_profile("w2", models=["llama-3"]))
        results = await g.by_model("gpt-4")
        assert {p.worker_id for p in results} == {"w1"}

    @pytest.mark.asyncio
    async def test_region_index(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", region="us-east-1"))
        await g.upsert(_profile("w2", region="eu-west-1"))
        results = await g.by_region("us-east-1")
        assert {p.worker_id for p in results} == {"w1"}

    @pytest.mark.asyncio
    async def test_healthy_profiles_filters_unhealthy(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", health_score=1.0))
        await g.upsert(_profile("w2", health_score=0.2))
        healthy = await g.healthy_profiles()
        ids = {p.worker_id for p in healthy}
        assert "w1" in ids
        assert "w2" not in ids

    @pytest.mark.asyncio
    async def test_query_gpu_filter(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", gpu=True))
        await g.upsert(_profile("w2", gpu=False))
        results = await g.query(requires_gpu=True)
        assert all(p.gpu_available for p in results)

    @pytest.mark.asyncio
    async def test_query_memory_filter(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", memory_gb=8.0))
        await g.upsert(_profile("w2", memory_gb=32.0))
        results = await g.query(min_memory_gb=16.0)
        assert all(p.memory_gb >= 16.0 for p in results)

    @pytest.mark.asyncio
    async def test_query_trust_filter(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", trust_score=0.9))
        await g.upsert(_profile("w2", trust_score=0.4))
        results = await g.query(min_trust_score=0.8)
        assert all(p.trust_score >= 0.8 for p in results)

    @pytest.mark.asyncio
    async def test_query_combined(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", skills=frozenset({"nlp"}), gpu=True, memory_gb=32.0))
        await g.upsert(_profile("w2", skills=frozenset({"nlp"}), gpu=False, memory_gb=8.0))
        await g.upsert(_profile("w3", skills=frozenset({"vision"}), gpu=True, memory_gb=32.0))
        results = await g.query(skills=["nlp"], requires_gpu=True, min_memory_gb=16.0)
        assert {p.worker_id for p in results} == {"w1"}

    @pytest.mark.asyncio
    async def test_count(self):
        g = CapabilityGraph()
        assert await g.count() == 0
        await g.upsert(_profile("w1"))
        await g.upsert(_profile("w2"))
        assert await g.count() == 2

    @pytest.mark.asyncio
    async def test_index_cleanup_on_update(self):
        g = CapabilityGraph()
        await g.upsert(_profile("w1", skills=frozenset({"nlp"})))
        # Update removes nlp, adds vision
        await g.upsert(_profile("w1", skills=frozenset({"vision"})))
        nlp_results = await g.by_skill("nlp")
        vision_results = await g.by_skill("vision")
        assert not any(p.worker_id == "w1" for p in nlp_results)
        assert any(p.worker_id == "w1" for p in vision_results)
