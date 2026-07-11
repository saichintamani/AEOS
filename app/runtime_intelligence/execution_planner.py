"""
Wave 9B.3.2 — Execution Planner

Full execution plan assembler. Takes a list of TaskRequirements and produces
an optimized, decision-annotated ExecutionPlan ready for dispatch.

ExecutionPlan = optimized ExecutionGraph + per-node ExecutionDecisions
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    ExecutionDecision,
    ExecutionGraph,
    TaskRequirements,
)
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.optimizer import ExecutionOptimizer
from app.runtime_intelligence.planner import DefaultTaskPlanner

logger = logging.getLogger(__name__)


@dataclass
class ExecutionPlan:
    graph: ExecutionGraph
    decisions: dict[str, ExecutionDecision] = field(default_factory=dict)  # task_id → decision

    @property
    def is_feasible(self) -> bool:
        """True if every node in the graph has a decision with a non-empty worker_id."""
        if not self.graph.nodes:
            return False
        return all(
            self.decisions.get(tid, ExecutionDecision(
                task_id=tid, worker_id="", strategy_name="", expected_utility=0.0
            )).worker_id
            for tid in self.graph.nodes
            if not self.graph.nodes[tid].metadata.get("skip")
            and not self.graph.nodes[tid].metadata.get("merged_into")
        )

    def summary(self) -> str:
        total = len(self.graph.nodes)
        placed = sum(1 for d in self.decisions.values() if d.worker_id)
        skipped = sum(
            1 for n in self.graph.nodes.values()
            if n.metadata.get("skip") or n.metadata.get("merged_into")
        )
        return (
            f"ExecutionPlan: {total} tasks, {placed} placed, {skipped} skipped, "
            f"feasible={self.is_feasible}"
        )


class ExecutionPlanner:
    """
    Assembles a full ExecutionPlan from requirements and available workers.

    Pipeline:
      1. DefaultTaskPlanner → ExecutionGraph (with parallel groups, critical path)
      2. ExecutionOptimizer → prune redundant tasks
      3. ExpectedUtilityDecisionEngine → decide worker for each task node
    """

    def __init__(
        self,
        decision_engine: ExpectedUtilityDecisionEngine | None = None,
    ) -> None:
        self._planner = DefaultTaskPlanner()
        self._optimizer = ExecutionOptimizer()
        self._decider = decision_engine or ExpectedUtilityDecisionEngine()

    async def plan(
        self,
        requirements: list[TaskRequirements],
        profiles: list[CapabilityProfile],
        *,
        completed_task_ids: set[str] | None = None,
        idempotent_task_types: set[str] | None = None,
    ) -> ExecutionPlan:
        # Step 1: Build graph
        graph = await self._planner.plan(requirements)

        # Step 2: Optimize
        graph, opt_report = await self._optimizer.optimize(
            graph,
            completed_task_ids=completed_task_ids,
            idempotent_task_types=idempotent_task_types,
        )

        # Step 3: Decide worker for each actionable task
        decisions: dict[str, ExecutionDecision] = {}
        for tid, node in graph.nodes.items():
            if node.metadata.get("skip") or node.metadata.get("merged_into"):
                continue
            decision = await self._decider.decide(node.requirements, profiles)
            decisions[tid] = decision

        plan = ExecutionPlan(graph=graph, decisions=decisions)

        logger.info(
            "ExecutionPlanner: %s | optimizer: %s",
            plan.summary(), opt_report.summary(),
        )
        return plan
