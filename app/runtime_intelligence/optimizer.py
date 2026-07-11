"""
Wave 9B.3.5 — Execution Optimizer

High-level coordinator that applies WorkflowOptimizer and re-runs the
planner's critical path / parallel groups after optimization so the
final graph reflects the optimized structure.
"""

from __future__ import annotations

import logging

from app.runtime_intelligence.contracts import ExecutionGraph, TaskRequirements
from app.runtime_intelligence.planner import DefaultTaskPlanner
from app.runtime_intelligence.workflow_optimizer import OptimizationReport, WorkflowOptimizer

logger = logging.getLogger(__name__)


class ExecutionOptimizer:
    """
    Optimizes an execution graph produced by the TaskPlanner.

    Usage:
        optimizer = ExecutionOptimizer()
        graph, report = await optimizer.optimize(graph)
    """

    def __init__(self) -> None:
        self._wf_optimizer = WorkflowOptimizer()
        self._planner = DefaultTaskPlanner()

    async def optimize(
        self,
        graph: ExecutionGraph,
        *,
        completed_task_ids: set[str] | None = None,
        idempotent_task_types: set[str] | None = None,
    ) -> tuple[ExecutionGraph, OptimizationReport]:
        graph, report = self._wf_optimizer.optimize(
            graph,
            completed_task_ids=completed_task_ids,
            idempotent_task_types=idempotent_task_types,
        )

        # Re-compute parallel groups and critical path on the optimized graph
        self._planner._analyze_parallelism(graph)
        self._planner._compute_critical_path(graph)

        logger.info(
            "ExecutionOptimizer: graph %s optimized — %s",
            graph.graph_id, report.summary(),
        )
        return graph, report
