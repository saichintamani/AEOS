"""Unit tests — AutonomousOptimizationLoop."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import ExecutionRecord, KnowledgeNodeType
from app.runtime_intelligence.knowledge_graph import KnowledgeGraph
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime.optimization_loop import AutonomousOptimizationLoop


def _rec(worker_id="w1", task_type="nlp", success=True,
         model="gpt-4", error_type="") -> ExecutionRecord:
    return ExecutionRecord(
        worker_id=worker_id,
        task_type=task_type,
        success=success,
        failed=not success,
        model_used=model,
        latency_ms=100.0,
        cost=0.01,
        error_type=error_type,
    )


class TestAutonomousOptimizationLoop:

    @pytest.mark.asyncio
    async def test_process_now_updates_learning(self):
        learning = DefaultLearningEngine()
        loop = AutonomousOptimizationLoop(learning_engine=learning)
        await loop.process_now([_rec(success=True)] * 5)
        rate = await learning.predict_success_rate("w1", "nlp")
        assert rate > 0.9

    @pytest.mark.asyncio
    async def test_process_now_adds_kg_nodes(self):
        kg = KnowledgeGraph()
        loop = AutonomousOptimizationLoop(knowledge_graph=kg)
        await loop.process_now([_rec(worker_id="w1", model="claude-3")])
        nodes, edges = await kg.count()
        assert nodes >= 2   # worker + model
        assert edges >= 1   # worker→model

    @pytest.mark.asyncio
    async def test_failure_adds_failure_node(self):
        kg = KnowledgeGraph()
        loop = AutonomousOptimizationLoop(knowledge_graph=kg)
        await loop.process_now([_rec(success=False, error_type="OOMError")])
        failures = await kg.query(node_type=KnowledgeNodeType.FAILURE)
        assert len(failures) >= 1
        assert failures[0].label == "OOMError"

    @pytest.mark.asyncio
    async def test_report_counts(self):
        loop = AutonomousOptimizationLoop()
        records = [_rec(worker_id=f"w{i}", task_type=f"task{i}") for i in range(5)]
        summary = await loop.process_now(records)
        assert summary.records_processed == 5
        assert summary.workers_updated == 5
        assert summary.models_updated == 5  # all have model="gpt-4"

    @pytest.mark.asyncio
    async def test_idempotent_kg_nodes(self):
        kg = KnowledgeGraph()
        loop = AutonomousOptimizationLoop(knowledge_graph=kg)
        # Same worker twice
        await loop.process_now([_rec(worker_id="w1"), _rec(worker_id="w1")])
        worker_nodes = await kg.nodes_by_type(KnowledgeNodeType.WORKER)
        # Should only have one worker node
        assert sum(1 for n in worker_nodes if n.node_id == "worker:w1") == 1

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self):
        loop = AutonomousOptimizationLoop()
        await loop.start()
        assert loop._running
        await loop.stop()
        assert not loop._running
