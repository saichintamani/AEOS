"""
Unit tests for Wave 9B.5.8 — Capability Federation.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.distributed.capability.federation import (
    CapabilityAdvertisement,
    CapabilityCategory,
    CapabilityFederator,
    CapabilityRegistry,
    EmbeddingCapability,
    GPUCapability,
    LLMCapability,
    MemoryCapability,
)
from app.runtime_intelligence.contracts import TaskRequirements


# ── helpers ───────────────────────────────────────────────────────────────────

def _llm_worker(wid: str = "llm-1", models=None, load: float = 0.1) -> CapabilityAdvertisement:
    return CapabilityAdvertisement(
        worker_id=wid,
        cpu_cores=8,
        memory_gb=16.0,
        current_load=load,
        llm=LLMCapability(
            models=models or ["claude-3-opus", "gpt-4"],
            max_context_tokens=100_000,
            supports_function_calling=True,
        ),
        has_tool_call=True,
        has_planning=True,
    )


def _gpu_worker(wid: str = "gpu-1") -> CapabilityAdvertisement:
    return CapabilityAdvertisement(
        worker_id=wid,
        cpu_cores=16,
        memory_gb=32.0,
        current_load=0.2,
        gpu=GPUCapability(device_count=2, vram_gb=40.0, cuda_version="12.1"),
        embedding=EmbeddingCapability(models=["text-embedding-3-large"], max_dimensions=3072),
        has_vision=True,
    )


def _memory_worker(wid: str = "mem-1") -> CapabilityAdvertisement:
    return CapabilityAdvertisement(
        worker_id=wid,
        cpu_cores=4,
        memory_gb=8.0,
        current_load=0.3,
        memory_store=MemoryCapability(backend="pgvector", supports_hybrid_search=True),
        has_rag=True,
        has_search=True,
    )


# ── CapabilityAdvertisement ───────────────────────────────────────────────────

class TestCapabilityAdvertisement:

    def test_llm_worker_categories(self):
        adv = _llm_worker()
        cats = adv.categories
        assert CapabilityCategory.LLM in cats
        assert CapabilityCategory.TOOL_CALL in cats
        assert CapabilityCategory.PLANNING in cats
        assert CapabilityCategory.GPU not in cats

    def test_gpu_worker_categories(self):
        adv = _gpu_worker()
        cats = adv.categories
        assert CapabilityCategory.GPU in cats
        assert CapabilityCategory.EMBEDDING in cats
        assert CapabilityCategory.VISION in cats

    def test_memory_worker_categories(self):
        adv = _memory_worker()
        cats = adv.categories
        assert CapabilityCategory.MEMORY in cats
        assert CapabilityCategory.RAG in cats
        assert CapabilityCategory.SEARCH in cats

    def test_worker_with_no_capabilities(self):
        adv = CapabilityAdvertisement(worker_id="bare")
        assert len(adv.categories) == 0

    def test_to_capability_profile_includes_models(self):
        adv = _llm_worker(models=["claude-3-opus"])
        profile = adv.to_capability_profile()
        assert "claude-3-opus" in profile.supported_models

    def test_to_capability_profile_includes_gpu(self):
        adv = _gpu_worker()
        profile = adv.to_capability_profile()
        assert profile.gpu_available is True
        assert profile.gpu_memory_gb == 40.0

    def test_to_capability_profile_skills_include_category_values(self):
        adv = _llm_worker()
        profile = adv.to_capability_profile()
        assert "llm" in profile.skills
        assert "tool_call" in profile.skills

    def test_advertised_at_defaults_to_now(self):
        before = time.monotonic()
        adv = CapabilityAdvertisement(worker_id="w1")
        after = time.monotonic()
        assert before <= adv.advertised_at <= after


# ── CapabilityRegistry ────────────────────────────────────────────────────────

class TestCapabilityRegistry:

    @pytest.mark.asyncio
    async def test_put_and_get(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        adv = _llm_worker()
        await registry.put(adv)
        result = await registry.get("llm-1")
        assert result is not None
        assert result.worker_id == "llm-1"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_unknown(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        result = await registry.get("unknown")
        assert result is None

    @pytest.mark.asyncio
    async def test_put_updates_advertised_at(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        adv = _llm_worker()
        adv.advertised_at = 0.0  # artificially old
        await registry.put(adv)
        stored = await registry.get("llm-1")
        assert stored is not None
        assert stored.advertised_at > 1.0  # refreshed

    @pytest.mark.asyncio
    async def test_stale_entry_not_returned_by_get(self):
        registry = CapabilityRegistry(ttl_s=0.05)  # 50ms TTL
        adv = _llm_worker()
        await registry.put(adv)
        await asyncio.sleep(0.1)
        result = await registry.get("llm-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_all_fresh_excludes_stale(self):
        registry = CapabilityRegistry(ttl_s=0.05)
        adv = _llm_worker()
        await registry.put(adv)
        await asyncio.sleep(0.1)
        fresh = await registry.all_fresh()
        assert len(fresh) == 0

    @pytest.mark.asyncio
    async def test_by_category_filters(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        await registry.put(_llm_worker("l1"))
        await registry.put(_gpu_worker("g1"))
        await registry.put(_memory_worker("m1"))

        llms = await registry.by_category(CapabilityCategory.LLM)
        assert len(llms) == 1
        assert llms[0].worker_id == "l1"

        gpus = await registry.by_category(CapabilityCategory.GPU)
        assert len(gpus) == 1
        assert gpus[0].worker_id == "g1"

    @pytest.mark.asyncio
    async def test_remove_deletes_entry(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        await registry.put(_llm_worker())
        await registry.remove("llm-1")
        result = await registry.get("llm-1")
        assert result is None

    @pytest.mark.asyncio
    async def test_evict_stale_removes_expired(self):
        registry = CapabilityRegistry(ttl_s=0.05)
        await registry.put(_llm_worker("old"))
        await asyncio.sleep(0.1)
        await registry.put(_llm_worker("new-worker"))
        evicted = await registry.evict_stale()
        assert evicted == 1  # only "old" was evicted
        assert registry.worker_count == 1

    @pytest.mark.asyncio
    async def test_worker_count(self):
        registry = CapabilityRegistry(ttl_s=60.0)
        assert registry.worker_count == 0
        await registry.put(_llm_worker())
        assert registry.worker_count == 1


# ── CapabilityFederator ───────────────────────────────────────────────────────

class TestCapabilityFederatorAdvertise:

    @pytest.mark.asyncio
    async def test_advertise_registers_worker(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        found = await fed.registry.get("l1")
        assert found is not None

    @pytest.mark.asyncio
    async def test_withdraw_removes_worker(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        await fed.withdraw("l1")
        found = await fed.registry.get("l1")
        assert found is None


class TestCapabilityFederatorDiscover:

    @pytest.mark.asyncio
    async def test_discover_all_without_filter(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        await fed.advertise(_gpu_worker("g1"))
        results = await fed.discover()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_discover_by_category(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        await fed.advertise(_gpu_worker("g1"))

        llms = await fed.discover(categories={CapabilityCategory.LLM})
        assert len(llms) == 1 and llms[0].worker_id == "l1"

        gpus = await fed.discover(categories={CapabilityCategory.GPU})
        assert len(gpus) == 1 and gpus[0].worker_id == "g1"

    @pytest.mark.asyncio
    async def test_discover_by_required_models(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1", models=["claude-3-opus"]))
        await fed.advertise(_llm_worker("l2", models=["gpt-4"]))

        result = await fed.discover(require_models=["claude-3-opus"])
        assert len(result) == 1
        assert result[0].worker_id == "l1"

    @pytest.mark.asyncio
    async def test_discover_intersection_of_categories(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))  # LLM only
        await fed.advertise(_memory_worker("m1"))  # MEMORY + RAG + SEARCH

        # Need both LLM and MEMORY — no single worker has both
        results = await fed.discover(
            categories={CapabilityCategory.LLM, CapabilityCategory.MEMORY}
        )
        assert len(results) == 0


class TestCapabilityFederatorMatch:

    @pytest.mark.asyncio
    async def test_match_returns_ranked_results(self):
        fed = CapabilityFederator()
        # Two LLM workers, one has target model
        await fed.advertise(_llm_worker("l1", models=["claude-3-opus"], load=0.1))
        await fed.advertise(_llm_worker("l2", models=["gpt-4"], load=0.2))

        req = TaskRequirements(
            task_type="inference",
            required_models=["claude-3-opus"],
        )
        matches = await fed.match(req)
        assert len(matches) >= 1
        assert matches[0].worker_id == "l1"  # has the model

    @pytest.mark.asyncio
    async def test_match_excludes_workers_missing_required_model(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1", models=["gpt-4"]))

        req = TaskRequirements(
            task_type="inference",
            required_models=["claude-3-sonnet"],
        )
        matches = await fed.match(req)
        assert len(matches) == 0   # l1 doesn't have the model

    @pytest.mark.asyncio
    async def test_match_excludes_workers_missing_gpu(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))   # no GPU
        await fed.advertise(_gpu_worker("g1"))   # has GPU

        req = TaskRequirements(
            task_type="train",
            requires_gpu=True,
        )
        matches = await fed.match(req)
        assert all(m.worker_id == "g1" for m in matches)

    @pytest.mark.asyncio
    async def test_match_excludes_workers_with_insufficient_memory(self):
        fed = CapabilityFederator()
        small = CapabilityAdvertisement(worker_id="small", memory_gb=2.0,
                                        llm=LLMCapability(models=["m1"]))
        large = CapabilityAdvertisement(worker_id="large", memory_gb=64.0,
                                        llm=LLMCapability(models=["m1"]))
        await fed.advertise(small)
        await fed.advertise(large)

        req = TaskRequirements(task_type="t", required_memory_gb=16.0)
        matches = await fed.match(req)
        assert all(m.worker_id == "large" for m in matches)

    @pytest.mark.asyncio
    async def test_match_score_between_zero_and_one(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        await fed.advertise(_gpu_worker("g1"))

        req = TaskRequirements(task_type="general")
        matches = await fed.match(req)
        for m in matches:
            assert 0.0 <= m.match_score <= 1.0

    @pytest.mark.asyncio
    async def test_match_sorted_by_score_descending(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1", load=0.1))
        await fed.advertise(_llm_worker("l2", load=0.9))  # high load = lower score

        req = TaskRequirements(task_type="t")
        matches = await fed.match(req)
        scores = [m.match_score for m in matches]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_profiles_returns_all_fresh(self):
        fed = CapabilityFederator()
        await fed.advertise(_llm_worker("l1"))
        await fed.advertise(_gpu_worker("g1"))

        profiles = await fed.profiles()
        assert len(profiles) == 2
        ids = {p.worker_id for p in profiles}
        assert ids == {"l1", "g1"}

    @pytest.mark.asyncio
    async def test_evict_stale_removes_expired(self):
        fed = CapabilityFederator(ttl_s=0.05)
        await fed.advertise(_llm_worker("old"))
        await asyncio.sleep(0.1)
        evicted = await fed.evict_stale()
        assert evicted == 1
        profiles = await fed.profiles()
        assert len(profiles) == 0
