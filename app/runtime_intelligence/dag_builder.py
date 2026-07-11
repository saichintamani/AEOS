"""
Wave 9B.3.2 — DAG Builder

Converts a flat list of TaskRequirements into a structured ExecutionGraph.

Dependency detection heuristics:
  - Same workflow_id + explicit step ordering by step_id
  - Required-models or required-skills overlaps that imply data flow
  - Caller-provided dependency hints in payload["depends_on"]

The builder does NOT schedule — it only structures the graph.
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.runtime_intelligence.contracts import (
    ExecutionGraph,
    TaskDependencyType,
    TaskNode,
    TaskRequirements,
)

logger = logging.getLogger(__name__)


class DAGBuilder:
    """
    Builds an ExecutionGraph from a list of TaskRequirements.

    Dependency resolution order:
      1. Explicit: payload["depends_on"] list of task_ids / step_ids
      2. Sequential: tasks in the same workflow with numeric step_ids
      3. Independent: everything else can parallelize
    """

    def build(
        self,
        requirements: Sequence[TaskRequirements],
        workflow_id: str = "",
    ) -> ExecutionGraph:
        graph = ExecutionGraph(workflow_id=workflow_id)

        # Create nodes
        step_map: dict[str, str] = {}   # step_id → task_id
        for req in requirements:
            node = TaskNode(
                task_id=req.task_id,
                task_type=req.task_type,
                requirements=req,
                can_parallelize=True,
                priority=self._priority_int(req.priority),
                payload=req.payload.copy(),
            )
            graph.add_node(node)
            if req.step_id:
                step_map[req.step_id] = req.task_id

        # Wire explicit depends_on
        for req in requirements:
            explicit: list[str] = req.payload.get("depends_on", [])
            for dep_ref in explicit:
                dep_task_id = dep_ref if dep_ref in graph.nodes else step_map.get(dep_ref)
                if dep_task_id and dep_task_id != req.task_id:
                    graph.add_edge(dep_task_id, req.task_id, TaskDependencyType.SEQUENTIAL)

        # Sequential ordering for tasks in same workflow sharing numeric step_ids
        if workflow_id:
            self._wire_sequential_steps(graph, requirements, step_map)

        logger.debug(
            "DAGBuilder: built graph %s — %d nodes, %d edges",
            graph.graph_id, len(graph.nodes), len(graph.edges),
        )
        return graph

    # ── Internal ──────────────────────────────────────────────────────────────

    def _wire_sequential_steps(
        self,
        graph: ExecutionGraph,
        requirements: Sequence[TaskRequirements],
        step_map: dict[str, str],
    ) -> None:
        """Wire step_0 → step_1 → step_2 for tasks with numeric step_ids."""
        numbered: list[tuple[int, str]] = []
        for req in requirements:
            try:
                idx = int(req.step_id)
                numbered.append((idx, req.task_id))
            except (ValueError, TypeError):
                continue

        numbered.sort(key=lambda x: x[0])
        for i in range(1, len(numbered)):
            prev_task_id = numbered[i - 1][1]
            curr_task_id = numbered[i][1]
            # Only add if edge doesn't already exist
            existing = {(f, t) for f, t, _ in graph.edges}
            if (prev_task_id, curr_task_id) not in existing:
                graph.add_edge(prev_task_id, curr_task_id, TaskDependencyType.SEQUENTIAL)
                graph.nodes[curr_task_id].can_parallelize = False

    @staticmethod
    def _priority_int(priority: str) -> int:
        return {"critical": 4, "high": 3, "normal": 2, "low": 1, "batch": 0}.get(priority, 2)
