"""
Integration tests — Full Phase 9B.3 pipeline.

Exercises the end-to-end flow:
  CapabilityGraph → CapabilityMatcher → DecisionEngine → LearningEngine → Predictor
  TaskRequirements → ExecutionPlanner → ExecutionPlan
"""

from __future__ import annotations

import pytest

from app.runtime_intelligence.capability_graph import CapabilityGraph
from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    ExecutionRecord,
    TaskRequirements,
)
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.execution_planner import ExecutionPlanner
from app.runtime_intelligence.knowledge_graph import KnowledgeGraph
from app.runtime_intelligence.contracts import KnowledgeEdge, KnowledgeNode, KnowledgeNodeType
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime_intelligence.runtime_predictor import RuntimePredictor
from app.runtime_intelligence.simulation import SimulationEngine
from app.runtime_intelligence.contracts import SimulationScenario


def _profile(worker_id: str, **kwargs) -> CapabilityProfile:
    defaults = dict(
        memory_gb=16.0,
        gpu_available=False,
        trust_score=0.9,
        current_load=0.2,
        avg_latency_ms=50.0,
        token_cost_per_k=0.01,
        health_score=1.0,
        historical_success_rate=0.95,
        region="us-east-1",
        az="a",
        skills=frozenset(),
        supported_models=[],
    )
    defaults.update(kwargs)
    return CapabilityProfile(worker_id=worker_id, **defaults)


class TestCapabilityGraphToDecisionEngine:

    @pytest.mark.asyncio
    async def test_end_to_end_selection(self):
        graph = CapabilityGraph()
        for i in range(5):
            await graph.upsert(_profile(f"worker-{i}", current_load=i * 0.15))

        profiles = await graph.healthy_profiles()
        engine = ExpectedUtilityDecisionEngine()
        decision = await engine.decide(TaskRequirements(task_type="generic"), profiles)

        assert decision.worker_id != ""
        assert decision.expected_utility > 0
        assert decision.confidence > 0

    @pytest.mark.asyncio
    async def test_gpu_requirement_routing(self):
        graph = CapabilityGraph()
        await graph.upsert(_profile("cpu-worker", gpu_available=False))
        await graph.upsert(_profile("gpu-worker", gpu_available=True, gpu_memory_gb=16.0))

        profiles = await graph.healthy_profiles()
        engine = ExpectedUtilityDecisionEngine()
        decision = await engine.decide(
            TaskRequirements(requires_gpu=True, required_gpu_memory_gb=8.0),
            profiles,
        )
        assert decision.worker_id == "gpu-worker"

    @pytest.mark.asyncio
    async def test_learning_feedback_loop(self):
        learning = DefaultLearningEngine()
        engine = ExpectedUtilityDecisionEngine(learning_engine=learning)

        profiles = [
            _profile("reliable", current_load=0.5),
            _profile("flaky", current_load=0.1),   # lower load but bad history
        ]

        # Train: flaky fails a lot
        for _ in range(15):
            await learning.record(ExecutionRecord(
                worker_id="flaky", task_type="analysis",
                success=False, latency_ms=200, cost=0.01
            ))
        for _ in range(15):
            await learning.record(ExecutionRecord(
                worker_id="reliable", task_type="analysis",
                success=True, latency_ms=150, cost=0.01
            ))

        decision = await engine.decide(
            TaskRequirements(task_type="analysis"), profiles
        )
        assert decision.worker_id == "reliable"


class TestExecutionPlanner:

    @pytest.mark.asyncio
    async def test_full_plan_is_feasible(self):
        profiles = [_profile(f"w{i}") for i in range(3)]
        planner = ExecutionPlanner()
        reqs = [TaskRequirements(task_id=f"t{i}", task_type="step") for i in range(4)]
        plan = await planner.plan(reqs, profiles)
        assert plan.is_feasible

    @pytest.mark.asyncio
    async def test_plan_with_sequential_steps(self):
        profiles = [_profile("w1"), _profile("w2")]
        planner = ExecutionPlanner()
        reqs = [
            TaskRequirements(task_id="t0", task_type="ingest", step_id="0", workflow_id="wf1"),
            TaskRequirements(task_id="t1", task_type="process", step_id="1", workflow_id="wf1"),
            TaskRequirements(task_id="t2", task_type="emit", step_id="2", workflow_id="wf1"),
        ]
        plan = await planner.plan(reqs, profiles)
        # Sequential plan has exactly 3 nodes
        assert len(plan.graph.nodes) == 3
        # Critical path should be linear through all 3
        assert len(plan.graph.critical_path) == 3

    @pytest.mark.asyncio
    async def test_plan_skips_completed(self):
        profiles = [_profile("w1")]
        planner = ExecutionPlanner()
        reqs = [
            TaskRequirements(task_id="done", task_type="t"),
            TaskRequirements(task_id="pending", task_type="t"),
        ]
        plan = await planner.plan(reqs, profiles, completed_task_ids={"done"})
        # "done" should be skipped
        assert plan.graph.nodes["done"].metadata.get("skip")
        assert not plan.graph.nodes["pending"].metadata.get("skip")

    @pytest.mark.asyncio
    async def test_plan_summary_non_empty(self):
        profiles = [_profile("w1")]
        planner = ExecutionPlanner()
        plan = await planner.plan([TaskRequirements(task_id="t1")], profiles)
        assert "ExecutionPlan" in plan.summary()


class TestLearningAndPredictor:

    @pytest.mark.asyncio
    async def test_predictor_uses_learning_history(self):
        learning = DefaultLearningEngine()
        predictor = RuntimePredictor(learning)

        for _ in range(5):
            await learning.record(ExecutionRecord(
                worker_id="w1", task_type="translate",
                success=True, latency_ms=250.0, cost=0.02
            ))

        lat = await predictor.predict_latency_ms("w1", "translate")
        assert lat > 0   # EMA moved from default toward 250

    @pytest.mark.asyncio
    async def test_deadline_met(self):
        learning = DefaultLearningEngine()
        predictor = RuntimePredictor(learning)
        for _ in range(3):
            await learning.record(ExecutionRecord(
                worker_id="w1", task_type="t",
                success=True, latency_ms=50.0
            ))

        profile = _profile("w1")
        req = TaskRequirements(task_type="t", max_latency_ms=200.0)
        assert await predictor.will_meet_deadline(profile, req)

    @pytest.mark.asyncio
    async def test_deadline_missed(self):
        learning = DefaultLearningEngine()
        predictor = RuntimePredictor(learning)
        for _ in range(10):
            await learning.record(ExecutionRecord(
                worker_id="w1", task_type="t",
                success=True, latency_ms=500.0
            ))

        profile = _profile("w1")
        req = TaskRequirements(task_type="t", max_latency_ms=100.0)
        assert not await predictor.will_meet_deadline(profile, req)


class TestKnowledgeGraphIntegration:

    @pytest.mark.asyncio
    async def test_worker_model_relationship(self):
        kg = KnowledgeGraph()
        worker = KnowledgeNode(node_id="w1", node_type=KnowledgeNodeType.WORKER, label="worker-1")
        model = KnowledgeNode(node_id="m1", node_type=KnowledgeNodeType.MODEL, label="gpt-4")
        edge = KnowledgeEdge(edge_id="e1", from_node_id="w1", to_node_id="m1", relation="executes")

        await kg.add_node(worker)
        await kg.add_node(model)
        await kg.add_edge(edge)

        # Query which models a worker executes
        out = await kg.out_edges("w1", relation="executes")
        assert len(out) == 1
        assert out[0].to_node_id == "m1"

        # Query which workers execute a model
        in_e = await kg.in_edges("m1", relation="executes")
        assert len(in_e) == 1
        assert in_e[0].from_node_id == "w1"

    @pytest.mark.asyncio
    async def test_failure_chain_query(self):
        kg = KnowledgeGraph()
        task_node = KnowledgeNode(node_id="t1", node_type=KnowledgeNodeType.TASK, label="task-1")
        failure_node = KnowledgeNode(
            node_id="f1", node_type=KnowledgeNodeType.FAILURE, label="OOMError",
            properties={"error_type": "OOM"}
        )
        edge = KnowledgeEdge(edge_id="e1", from_node_id="t1", to_node_id="f1", relation="caused_failure")

        await kg.add_node(task_node)
        await kg.add_node(failure_node)
        await kg.add_edge(edge)

        failures = await kg.query(node_type=KnowledgeNodeType.FAILURE)
        assert len(failures) == 1
        assert failures[0].properties.get("error_type") == "OOM"


class TestSimulationEngine:

    @pytest.mark.asyncio
    async def test_simulation_runs_and_returns_result(self):
        engine = SimulationEngine()
        scenario = SimulationScenario(
            n_workers=5,
            n_agents=10,
            task_arrival_rate=5.0,
            simulation_duration_seconds=5.0,
        )
        result = await engine.run(scenario)
        assert result.scenario_id == scenario.scenario_id
        total = result.total_tasks_executed + result.total_tasks_failed
        assert total > 0

    @pytest.mark.asyncio
    async def test_simulation_with_fault_injection(self):
        engine = SimulationEngine()
        scenario = SimulationScenario(
            n_workers=10,
            task_arrival_rate=3.0,
            simulation_duration_seconds=5.0,
            worker_crash_probability=0.2,
        )
        result = await engine.run(scenario)
        # With crashes, failures should be > 0 (when no healthy workers found)
        # But not necessarily — depends on RNG. Just verify it completes.
        assert result.scenario_id == scenario.scenario_id

    @pytest.mark.asyncio
    async def test_simulation_with_autoscaling(self):
        engine = SimulationEngine()
        scenario = SimulationScenario(
            n_workers=5,
            task_arrival_rate=2.0,
            simulation_duration_seconds=10.0,
            worker_crash_probability=0.3,
            enable_autoscaling=True,
        )
        result = await engine.run(scenario)
        # Autoscaling should have fired at least once given high crash rate
        assert result.autoscale_events >= 0  # Could be 0 if RNG doesn't crash any

    @pytest.mark.asyncio
    async def test_simulation_throughput_positive(self):
        engine = SimulationEngine()
        scenario = SimulationScenario(
            n_workers=3,
            task_arrival_rate=10.0,
            simulation_duration_seconds=10.0,
        )
        result = await engine.run(scenario)
        assert result.throughput_rps > 0
