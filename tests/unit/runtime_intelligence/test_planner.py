"""Unit tests — DefaultTaskPlanner."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import TaskRequirements
from app.runtime_intelligence.planner import DefaultTaskPlanner


def _req(task_id: str, step_id: str = "", workflow_id: str = "wf1",
         duration_ms: float = 100.0, depends_on: list[str] | None = None) -> TaskRequirements:
    payload = {}
    if depends_on:
        payload["depends_on"] = depends_on
    return TaskRequirements(
        task_id=task_id,
        task_type="t",
        step_id=step_id,
        workflow_id=workflow_id,
        payload=payload,
    )


class TestDefaultTaskPlanner:

    @pytest.mark.asyncio
    async def test_empty_requirements(self):
        p = DefaultTaskPlanner()
        g = await p.plan([])
        assert len(g.nodes) == 0

    @pytest.mark.asyncio
    async def test_parallel_groups_independent_tasks(self):
        p = DefaultTaskPlanner()
        # Three independent tasks → all in one parallel group
        g = await p.plan([_req("a"), _req("b"), _req("c")])
        assert any(len(group) == 3 for group in g.parallel_groups)

    @pytest.mark.asyncio
    async def test_parallel_groups_with_dependency(self):
        p = DefaultTaskPlanner()
        reqs = [
            _req("t1"),
            _req("t2"),
            _req("t3", depends_on=["t1", "t2"]),
        ]
        g = await p.plan(reqs)
        # t1 and t2 should be in same group (level 0), t3 in own group (level 1)
        assert len(g.parallel_groups) == 2

    @pytest.mark.asyncio
    async def test_critical_path_linear(self):
        p = DefaultTaskPlanner()
        reqs = [
            _req("t1"),
            _req("t2", depends_on=["t1"]),
            _req("t3", depends_on=["t2"]),
        ]
        g = await p.plan(reqs)
        assert g.critical_path == ["t1", "t2", "t3"]

    @pytest.mark.asyncio
    async def test_workflow_id_propagated(self):
        p = DefaultTaskPlanner()
        reqs = [_req("t1", workflow_id="wf-42")]
        g = await p.plan(reqs)
        assert g.workflow_id == "wf-42"

    @pytest.mark.asyncio
    async def test_estimated_cost_zero_for_infinite_budget(self):
        p = DefaultTaskPlanner()
        reqs = [_req("t1")]
        g = await p.plan(reqs)
        assert g.estimated_total_cost == 0.0
