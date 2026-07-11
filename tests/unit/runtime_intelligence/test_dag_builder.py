"""Unit tests — DAGBuilder."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import TaskDependencyType, TaskRequirements
from app.runtime_intelligence.dag_builder import DAGBuilder


def _req(task_id: str = "", step_id: str = "", task_type: str = "t",
         workflow_id: str = "", depends_on: list[str] | None = None,
         priority: str = "normal") -> TaskRequirements:
    payload = {}
    if depends_on:
        payload["depends_on"] = depends_on
    return TaskRequirements(
        task_id=task_id or f"tid-{step_id or task_type}",
        task_type=task_type,
        step_id=step_id,
        workflow_id=workflow_id,
        priority=priority,
        payload=payload,
    )


class TestDAGBuilder:

    def test_single_task_no_edges(self):
        b = DAGBuilder()
        g = b.build([_req("t1")])
        assert len(g.nodes) == 1
        assert len(g.edges) == 0

    def test_explicit_depends_on(self):
        b = DAGBuilder()
        t1 = _req("t1")
        t2 = _req("t2", depends_on=["t1"])
        g = b.build([t1, t2])
        assert ("t1", "t2", TaskDependencyType.SEQUENTIAL) in g.edges

    def test_sequential_step_ids(self):
        b = DAGBuilder()
        t0 = _req("ta", step_id="0", workflow_id="wf1")
        t1 = _req("tb", step_id="1", workflow_id="wf1")
        t2 = _req("tc", step_id="2", workflow_id="wf1")
        g = b.build([t0, t1, t2], workflow_id="wf1")
        edges_from_to = [(f, t) for f, t, _ in g.edges]
        assert ("ta", "tb") in edges_from_to
        assert ("tb", "tc") in edges_from_to

    def test_non_sequential_step_ids_not_wired(self):
        """Non-numeric step ids don't get auto-wired."""
        b = DAGBuilder()
        t1 = _req("t1", step_id="alpha", workflow_id="wf1")
        t2 = _req("t2", step_id="beta", workflow_id="wf1")
        g = b.build([t1, t2], workflow_id="wf1")
        assert len(g.edges) == 0

    def test_roots(self):
        b = DAGBuilder()
        t1 = _req("t1")
        t2 = _req("t2", depends_on=["t1"])
        g = b.build([t1, t2])
        roots = g.roots()
        assert len(roots) == 1
        assert roots[0].task_id == "t1"

    def test_leaves(self):
        b = DAGBuilder()
        t1 = _req("t1")
        t2 = _req("t2", depends_on=["t1"])
        g = b.build([t1, t2])
        leaves = g.leaves()
        assert len(leaves) == 1
        assert leaves[0].task_id == "t2"

    def test_priority_mapping(self):
        b = DAGBuilder()
        reqs = [
            _req("tc", priority="critical"),
            _req("tb", priority="batch"),
        ]
        g = b.build(reqs)
        assert g.nodes["tc"].priority == 4
        assert g.nodes["tb"].priority == 0

    def test_empty_requirements(self):
        b = DAGBuilder()
        g = b.build([])
        assert len(g.nodes) == 0
        assert len(g.edges) == 0
