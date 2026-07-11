"""
Wave 9B.5.10 — E2E Demo Tests

Verifies the full distributed runtime demo runs successfully.
"""

from __future__ import annotations

import asyncio

import pytest

from app.distributed.demo.e2e_demo import DistributedDemo, DemoResult, run_demo


class TestDemoResult:

    def test_success_rate_zero_when_no_tasks(self):
        result = DemoResult(workflow_id="test")
        assert result.success_rate == 0.0

    def test_success_rate_calculation(self):
        result = DemoResult(workflow_id="test",
                            tasks_submitted=10, tasks_succeeded=8)
        assert result.success_rate == 0.8


class TestDistributedDemo:

    @pytest.mark.asyncio
    async def test_bootstrap_elects_raft_leader(self):
        demo = DistributedDemo()
        leader_id = await demo.bootstrap()
        assert leader_id == "coord-1"

    @pytest.mark.asyncio
    async def test_bootstrap_registers_all_workers(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        stats = demo.scheduler.cluster_stats()
        assert stats["workers"] == 5   # research, planner, reasoner, reviewer, memory

    @pytest.mark.asyncio
    async def test_bootstrap_federates_all_workers(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        profiles = await demo.federator.profiles()
        assert len(profiles) == 5

    @pytest.mark.asyncio
    async def test_run_workflow_returns_results(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        results = await demo.run_workflow("test-wf")
        assert len(results) == 5  # one per task

    @pytest.mark.asyncio
    async def test_run_workflow_majority_succeed(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        results = await demo.run_workflow("test-wf")
        succeeded = sum(1 for r in results if "error" not in r)
        assert succeeded >= 3   # at least 3/5 should succeed

    @pytest.mark.asyncio
    async def test_chaos_crashes_one_worker(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        events = await demo.run_chaos()
        crash_events = [e for e in events if e.startswith("WORKER_CRASH")]
        assert len(crash_events) == 1
        assert "research" in crash_events[0]

    @pytest.mark.asyncio
    async def test_chaos_cluster_shrinks_after_crash(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        await demo.run_chaos()
        stats = demo.scheduler.cluster_stats()
        assert stats["workers"] == 4   # research crashed

    @pytest.mark.asyncio
    async def test_fallback_task_dispatched_after_crash(self):
        demo = DistributedDemo()
        await demo.bootstrap()
        events = await demo.run_chaos()
        fallback = [e for e in events if e.startswith("FALLBACK_TASK")]
        assert len(fallback) == 1
        assert "ok" in fallback[0]

    @pytest.mark.asyncio
    async def test_collect_metrics_structure(self):
        demo = DistributedDemo()
        leader = await demo.bootstrap()
        results = await demo.run_workflow("test-wf")
        metrics = await demo.collect_metrics(leader, results)
        assert metrics.raft_leader == "coord-1"
        assert metrics.workers_registered >= 0
        assert metrics.tasks_submitted == len(results)


class TestRunDemo:

    @pytest.mark.asyncio
    async def test_run_demo_returns_result(self):
        result = await run_demo(verbose=False)
        assert isinstance(result, DemoResult)

    @pytest.mark.asyncio
    async def test_run_demo_has_nonzero_success_rate(self):
        result = await run_demo(verbose=False)
        assert result.success_rate > 0.0

    @pytest.mark.asyncio
    async def test_run_demo_raft_leader_set(self):
        result = await run_demo(verbose=False)
        assert result.raft_leader != ""

    @pytest.mark.asyncio
    async def test_run_demo_chaos_events_recorded(self):
        result = await run_demo(verbose=False)
        # Chaos should have fired
        assert len(result.chaos_events) >= 1

    @pytest.mark.asyncio
    async def test_run_demo_total_latency_positive(self):
        result = await run_demo(verbose=False)
        assert result.total_latency_ms > 0
