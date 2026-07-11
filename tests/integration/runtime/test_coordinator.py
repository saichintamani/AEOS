"""
Integration tests — RuntimeCoordinator + AEOSRuntime SDK.

Exercises the full Phase 9B.4 pipeline:
  WorkflowDefinition → WorkflowCompiler → ExecutionPlanner → Dispatch
  → ExecutionMonitor → OptimizationLoop → TelemetryBus
"""

from __future__ import annotations

import asyncio
import pytest

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime.coordinator import RuntimeCoordinator
from app.runtime.execution_monitor import LiveState
from app.runtime.sdk import AEOSRuntime
from app.runtime.telemetry_bus import TelemetryEventType
from app.runtime.workflow_compiler import TaskSpec, WorkflowDefinition


def _profile(worker_id: str, **kwargs) -> CapabilityProfile:
    defaults = dict(
        memory_gb=16.0, gpu_available=False, trust_score=0.9,
        current_load=0.2, avg_latency_ms=50.0, token_cost_per_k=0.01,
        health_score=1.0, historical_success_rate=0.95,
        region="us-east-1", az="a",
    )
    defaults.update(kwargs)
    return CapabilityProfile(worker_id=worker_id, **defaults)


class TestRuntimeCoordinator:

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        coord = RuntimeCoordinator()
        await coord.start()
        assert coord.optimization_loop._running
        await coord.stop()
        assert not coord.optimization_loop._running

    @pytest.mark.asyncio
    async def test_submit_task_tracked(self):
        coord = RuntimeCoordinator()
        await coord.start()
        await coord.capability_graph.upsert(_profile("w1"))

        req = TaskRequirements(task_id="t-1", task_type="generic")
        await coord.submit_task(req)
        await asyncio.sleep(0.2)   # let dispatch run

        state = await coord.monitor.get("t-1")
        assert state is not None
        assert state.state in (LiveState.COMPLETED, LiveState.FAILED, LiveState.RUNNING)
        await coord.stop()

    @pytest.mark.asyncio
    async def test_registered_handler_executed(self):
        coord = RuntimeCoordinator()
        await coord.start()
        await coord.capability_graph.upsert(_profile("w1"))

        results = []

        async def handler(req, _):
            results.append(req.task_id)
            return {"done": True}

        coord.register_agent("my-task", handler)
        req = TaskRequirements(task_id="t-2", task_type="my-task")
        await coord.submit_task(req)
        await asyncio.sleep(0.3)

        assert "t-2" in results
        state = await coord.monitor.get("t-2")
        assert state.state == LiveState.COMPLETED
        await coord.stop()

    @pytest.mark.asyncio
    async def test_telemetry_events_emitted(self):
        coord = RuntimeCoordinator()
        await coord.start()
        await coord.capability_graph.upsert(_profile("w1"))

        events = []

        async def handler(e):
            events.append(e.event_type)

        await coord.telemetry_bus.subscribe(None, handler)

        req = TaskRequirements(task_id="t-3", task_type="generic")
        await coord.submit_task(req)
        await asyncio.sleep(0.3)

        assert TelemetryEventType.TASK_STARTED in events
        assert (TelemetryEventType.TASK_COMPLETED in events
                or TelemetryEventType.TASK_FAILED in events)
        await coord.stop()

    @pytest.mark.asyncio
    async def test_submit_workflow(self):
        coord = RuntimeCoordinator()
        await coord.start()
        await coord.capability_graph.upsert(_profile("w1"))

        defn = WorkflowDefinition(
            workflow_id="wf-test",
            tasks=[
                TaskSpec(task_id="step-a", task_type="ingest"),
                TaskSpec(task_id="step-b", task_type="process", depends_on=["step-a"]),
            ],
        )
        wf_id = await coord.submit_workflow(defn)
        assert wf_id == "wf-test"
        await asyncio.sleep(0.4)
        await coord.stop()

    @pytest.mark.asyncio
    async def test_optimization_loop_receives_records(self):
        coord = RuntimeCoordinator()
        await coord.start()
        await coord.capability_graph.upsert(_profile("w1"))

        async def handler(req, _):
            return {}

        coord.register_agent("learn-task", handler)
        for i in range(5):
            req = TaskRequirements(task_id=f"lt-{i}", task_type="learn-task")
            await coord.submit_task(req)

        await asyncio.sleep(0.5)

        # After processing, learning engine should have records for "learn-task"
        rate = await coord.optimization_loop._learning.predict_success_rate("w1", "learn-task")
        assert rate > 0.0
        await coord.stop()


class TestAEOSRuntime:

    @pytest.mark.asyncio
    async def test_context_manager_lifecycle(self):
        async with AEOSRuntime() as runtime:
            assert runtime._started

    @pytest.mark.asyncio
    async def test_register_worker_and_submit(self):
        async with AEOSRuntime() as runtime:
            runtime.register_worker(_profile("sdk-worker"))
            await asyncio.sleep(0.05)  # let upsert task run

            results = []

            async def handler(req, _):
                results.append(req.task_id)
                return {"ok": True}

            runtime.register_agent("sdk-task", handler)
            await runtime.submit_task(TaskRequirements(task_id="sdk-t1", task_type="sdk-task"))
            await asyncio.sleep(0.3)
            # Task may or may not have a worker by now — just verify no crash

    @pytest.mark.asyncio
    async def test_submit_workflow_returns_id(self):
        async with AEOSRuntime() as runtime:
            runtime.register_worker(_profile("sdk-w1"))
            await asyncio.sleep(0.05)

            defn = WorkflowDefinition(
                workflow_id="sdk-wf-1",
                tasks=[TaskSpec(task_id="t1", task_type="step")],
            )
            wf_id = await runtime.submit_workflow(defn)
            assert wf_id == "sdk-wf-1"


class TestLifecycleController:

    @pytest.mark.asyncio
    async def test_start_stop_order(self):
        from app.runtime.lifecycle_controller import LifecycleController, LifecycleState

        order = []

        class Comp:
            def __init__(self, name):
                self.name = name
            async def start(self):
                order.append(f"start:{self.name}")
            async def stop(self):
                order.append(f"stop:{self.name}")

        lc = LifecycleController()
        lc.register("alpha", Comp("alpha"), start_order=10)
        lc.register("beta", Comp("beta"), start_order=20)

        await lc.start()
        assert lc.state == LifecycleState.RUNNING
        assert order == ["start:alpha", "start:beta"]

        await lc.stop()
        assert lc.state == LifecycleState.STOPPED
        # Should stop in reverse order
        assert order[2:] == ["stop:beta", "stop:alpha"]

    @pytest.mark.asyncio
    async def test_failed_start_sets_failed_state(self):
        from app.runtime.lifecycle_controller import LifecycleController, LifecycleState

        class Broken:
            async def start(self):
                raise RuntimeError("broken")

        lc = LifecycleController()
        lc.register("broken", Broken())

        with pytest.raises(RuntimeError):
            await lc.start()

        assert lc.state == LifecycleState.FAILED


class TestRuntimeProfiler:

    @pytest.mark.asyncio
    async def test_measure_records_duration(self):
        from app.runtime.runtime_profiler import RuntimeProfiler
        profiler = RuntimeProfiler()
        async with profiler.measure("test-op"):
            await asyncio.sleep(0.01)
        report = profiler.report()
        assert len(report.records) == 1
        assert report.records[0].name == "test-op"
        assert report.records[0].duration_ms >= 1.0

    @pytest.mark.asyncio
    async def test_summary_statistics(self):
        from app.runtime.runtime_profiler import RuntimeProfiler
        profiler = RuntimeProfiler()
        for i in range(5):
            profiler.record("op", duration_ms=float(i * 10 + 10))
        report = profiler.report()
        summary = report.summary("op")
        assert summary["count"] == 5
        assert summary["min_ms"] <= summary["avg_ms"] <= summary["max_ms"]

    @pytest.mark.asyncio
    async def test_reset_clears_records(self):
        from app.runtime.runtime_profiler import RuntimeProfiler
        profiler = RuntimeProfiler()
        profiler.record("op", 100.0)
        profiler.reset()
        assert len(profiler.report().records) == 0

    @pytest.mark.asyncio
    async def test_all_summaries(self):
        from app.runtime.runtime_profiler import RuntimeProfiler
        profiler = RuntimeProfiler()
        profiler.record("op-a", 10.0)
        profiler.record("op-b", 20.0)
        profiler.record("op-a", 15.0)
        summaries = profiler.report().all_summaries()
        names = [s["name"] for s in summaries]
        assert "op-a" in names
        assert "op-b" in names
        a_sum = next(s for s in summaries if s["name"] == "op-a")
        assert a_sum["count"] == 2
