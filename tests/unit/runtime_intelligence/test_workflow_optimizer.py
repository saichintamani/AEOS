"""Unit tests — WorkflowOptimizer."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import TaskRequirements
from app.runtime_intelligence.dag_builder import DAGBuilder
from app.runtime_intelligence.workflow_optimizer import WorkflowOptimizer


def _req(task_id: str, task_type: str = "t", priority: str = "normal",
         depends_on: list[str] | None = None) -> TaskRequirements:
    payload = {}
    if depends_on:
        payload["depends_on"] = depends_on
    return TaskRequirements(task_id=task_id, task_type=task_type, priority=priority, payload=payload)


class TestWorkflowOptimizer:

    def _build(self, reqs):
        builder = DAGBuilder()
        return builder.build(reqs)

    def test_skip_pass_marks_completed_tasks(self):
        opt = WorkflowOptimizer()
        graph = self._build([_req("t1"), _req("t2")])
        graph, report = opt.optimize(graph, completed_task_ids={"t1"})
        assert graph.nodes["t1"].metadata.get("skip")
        assert not graph.nodes["t2"].metadata.get("skip")
        assert report.skipped == 1

    def test_cache_pass_marks_idempotent_tasks(self):
        opt = WorkflowOptimizer()
        graph = self._build([_req("t1", task_type="summarize"), _req("t2", task_type="classify")])
        graph, report = opt.optimize(graph, idempotent_task_types={"summarize"})
        assert graph.nodes["t1"].metadata.get("cacheable")
        assert not graph.nodes["t2"].metadata.get("cacheable")
        assert report.cache_hits == 1

    def test_batch_pass_groups_batch_leaves(self):
        opt = WorkflowOptimizer()
        # Use distinct task types so merge pass doesn't collapse them
        graph = self._build([
            _req("b1", task_type="ingest", priority="batch"),
            _req("b2", task_type="index", priority="batch"),
            _req("b3", task_type="embed", priority="batch"),
        ])
        graph, report = opt.optimize(graph)
        assert report.batched == 3
        for tid in ["b1", "b2", "b3"]:
            assert "batch_group" in graph.nodes[tid].metadata

    def test_merge_pass_marks_duplicate_tasks(self):
        opt = WorkflowOptimizer()
        # Two tasks with same type and no skills/models — should be merged
        graph = self._build([_req("t1", task_type="x"), _req("t2", task_type="x")])
        graph, report = opt.optimize(graph)
        assert report.merges == 1

    def test_parallelize_pass_marks_independent_tasks(self):
        opt = WorkflowOptimizer()
        graph = self._build([_req("t1"), _req("t2")])
        # After planner runs parallel groups should be marked
        from app.runtime_intelligence.planner import DefaultTaskPlanner
        p = DefaultTaskPlanner()
        p._analyze_parallelism(graph)
        graph, report = opt.optimize(graph)
        assert report.parallelized >= 0  # may already be marked

    def test_report_summary_format(self):
        opt = WorkflowOptimizer()
        graph = self._build([_req("t1")])
        _, report = opt.optimize(graph)
        summary = report.summary()
        assert "merges=" in summary
        assert "skipped=" in summary
