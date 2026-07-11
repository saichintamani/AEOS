"""
Wave 9B.3.2 — Intelligent Task Planner

Orchestrates the full planning pipeline:
  TaskRequirements → DAGBuilder → DependencyAnalyzer → ParallelizationPlanner
  → MergePlanner → RetryPlanner → ExecutionGraph

DefaultTaskPlanner is the concrete implementation of the TaskPlanner ABC.
"""

from __future__ import annotations

import logging
from typing import Sequence

from app.runtime_intelligence.contracts import (
    ExecutionGraph,
    TaskDependencyType,
    TaskNode,
    TaskPlanner,
    TaskRequirements,
)
from app.runtime_intelligence.dag_builder import DAGBuilder

logger = logging.getLogger(__name__)

# Max tasks that can share a parallel group
_MAX_PARALLEL_GROUP = 32


class DefaultTaskPlanner(TaskPlanner):
    """
    Full planning pipeline that converts task requirements into an optimized
    ExecutionGraph with parallel groups and critical path annotated.
    """

    def __init__(self) -> None:
        self._dag_builder = DAGBuilder()

    async def plan(self, requirements: list[TaskRequirements]) -> ExecutionGraph:
        if not requirements:
            return ExecutionGraph()

        workflow_id = requirements[0].workflow_id if requirements else ""
        graph = self._dag_builder.build(requirements, workflow_id=workflow_id)

        self._analyze_parallelism(graph)
        self._compute_critical_path(graph)
        self._estimate_cost(graph)

        logger.info(
            "TaskPlanner: planned %d tasks — %d parallel groups, critical_path_len=%d",
            len(graph.nodes),
            len(graph.parallel_groups),
            len(graph.critical_path),
        )
        return graph

    # ── Planning stages ───────────────────────────────────────────────────────

    def _analyze_parallelism(self, graph: ExecutionGraph) -> None:
        """
        Topological level assignment. Tasks at the same level with no
        mutual dependency form a parallel group.
        """
        levels: dict[str, int] = {}

        def level_of(task_id: str) -> int:
            if task_id in levels:
                return levels[task_id]
            node = graph.nodes[task_id]
            if not node.dependencies:
                levels[task_id] = 0
                return 0
            max_dep = max(level_of(dep) for dep in node.dependencies)
            levels[task_id] = max_dep + 1
            return levels[task_id]

        for tid in graph.nodes:
            level_of(tid)

        # Group by level
        by_level: dict[int, list[str]] = {}
        for tid, lvl in levels.items():
            by_level.setdefault(lvl, []).append(tid)

        graph.parallel_groups = [
            group for _, group in sorted(by_level.items())
            if len(group) > 0
        ]

        # Mark parallelizable nodes
        for group in graph.parallel_groups:
            if len(group) > 1:
                for tid in group:
                    graph.nodes[tid].can_parallelize = True

    def _compute_critical_path(self, graph: ExecutionGraph) -> None:
        """Longest path through the DAG by estimated_duration_ms."""
        if not graph.nodes:
            return

        # dp[task_id] = (duration_on_critical_path, predecessor_task_id | None)
        dp: dict[str, tuple[float, str | None]] = {}

        def longest_path(task_id: str) -> float:
            if task_id in dp:
                return dp[task_id][0]
            node = graph.nodes[task_id]
            if not node.dependencies:
                dp[task_id] = (node.estimated_duration_ms, None)
                return node.estimated_duration_ms
            best_pred, best_dur = None, -1.0
            for dep_id in node.dependencies:
                dur = longest_path(dep_id) + node.estimated_duration_ms
                if dur > best_dur:
                    best_dur, best_pred = dur, dep_id
            if best_dur < 0:
                best_dur = node.estimated_duration_ms
            dp[task_id] = (best_dur, best_pred)
            return best_dur

        for tid in graph.nodes:
            longest_path(tid)

        # Find leaf with highest accumulated duration
        leaves = [n.task_id for n in graph.nodes.values() if not n.dependents]
        if not leaves:
            return

        end = max(leaves, key=lambda t: dp[t][0])
        graph.estimated_total_duration_ms = dp[end][0]

        # Trace back critical path
        path: list[str] = []
        cur: str | None = end
        while cur is not None:
            path.append(cur)
            cur = dp[cur][1]
        graph.critical_path = list(reversed(path))

    def _estimate_cost(self, graph: ExecutionGraph) -> None:
        total = 0.0
        for node in graph.nodes.values():
            req = node.requirements
            # Rough estimate: max_cost as a proxy if provided
            if req.max_cost < float("inf"):
                total += req.max_cost * 0.5  # assume ~50% of budget used
        graph.estimated_total_cost = round(total, 4)
