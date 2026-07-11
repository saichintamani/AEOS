"""
Wave 9B.3.5 — Workflow Optimizer

Transforms an ExecutionGraph to reduce cost and latency before execution:

  MergePass       — merge redundant tasks with identical type+skills
  ParallelizePass — promote independent tasks to same parallel level
  CachePass       — mark tasks whose results can be reused (idempotent)
  SkipPass        — mark tasks superseded by completed ones
  BatchPass       — group batch-priority tasks into a single virtual task

WorkflowOptimizer — runs all passes in order, returns optimized graph + report
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import (
    ExecutionGraph,
    TaskDependencyType,
    TaskNode,
)

logger = logging.getLogger(__name__)


@dataclass
class OptimizationReport:
    merges: int = 0
    parallelized: int = 0
    cache_hits: int = 0
    skipped: int = 0
    batched: int = 0

    def summary(self) -> str:
        return (
            f"merges={self.merges}, parallelized={self.parallelized}, "
            f"cache_hits={self.cache_hits}, skipped={self.skipped}, batched={self.batched}"
        )


class WorkflowOptimizer:
    """
    Runs optimization passes on an ExecutionGraph.
    Passes are non-destructive: they annotate nodes via metadata rather
    than removing them, so the planner can decide what to do.
    """

    def optimize(
        self,
        graph: ExecutionGraph,
        completed_task_ids: set[str] | None = None,
        idempotent_task_types: set[str] | None = None,
    ) -> tuple[ExecutionGraph, OptimizationReport]:
        report = OptimizationReport()
        completed_task_ids = completed_task_ids or set()
        idempotent_task_types = idempotent_task_types or set()

        self._merge_pass(graph, report)
        self._parallelize_pass(graph, report)
        self._cache_pass(graph, report, idempotent_task_types)
        self._skip_pass(graph, report, completed_task_ids)
        self._batch_pass(graph, report)

        logger.info("WorkflowOptimizer: %s", report.summary())
        return graph, report

    # ── Passes ────────────────────────────────────────────────────────────────

    def _merge_pass(self, graph: ExecutionGraph, report: OptimizationReport) -> None:
        """Merge tasks with identical (task_type, required_skills, required_models)."""
        seen: dict[tuple, str] = {}
        to_merge: list[tuple[str, str]] = []   # (duplicate_id, canonical_id)

        for tid, node in list(graph.nodes.items()):
            req = node.requirements
            key = (
                req.task_type,
                frozenset(req.required_skills),
                tuple(sorted(req.required_models)),
            )
            if key in seen:
                canonical = seen[key]
                to_merge.append((tid, canonical))
            else:
                seen[key] = tid

        for dup_id, canonical_id in to_merge:
            dup_node = graph.nodes[dup_id]
            # Re-point dependents of duplicate to canonical
            for dep_id in dup_node.dependents:
                dep_node = graph.nodes.get(dep_id)
                if dep_node and dup_id in dep_node.dependencies:
                    dep_node.dependencies.remove(dup_id)
                    if canonical_id not in dep_node.dependencies:
                        dep_node.dependencies.append(canonical_id)
                    graph.add_edge(canonical_id, dep_id, TaskDependencyType.SEQUENTIAL)
            # Mark as merged
            dup_node.metadata["merged_into"] = canonical_id
            report.merges += 1

    def _parallelize_pass(self, graph: ExecutionGraph, report: OptimizationReport) -> None:
        """Mark independent tasks (no dependency path between them) as parallelizable."""
        for group in graph.parallel_groups:
            if len(group) > 1:
                for tid in group:
                    node = graph.nodes.get(tid)
                    if node and not node.can_parallelize:
                        node.can_parallelize = True
                        report.parallelized += 1

    def _cache_pass(
        self,
        graph: ExecutionGraph,
        report: OptimizationReport,
        idempotent_types: set[str],
    ) -> None:
        """Mark idempotent tasks as cacheable."""
        for node in graph.nodes.values():
            if node.task_type in idempotent_types:
                node.metadata["cacheable"] = True
                report.cache_hits += 1

    def _skip_pass(
        self,
        graph: ExecutionGraph,
        report: OptimizationReport,
        completed_ids: set[str],
    ) -> None:
        """Mark already-completed tasks as skip."""
        for tid in completed_ids:
            node = graph.nodes.get(tid)
            if node:
                node.metadata["skip"] = True
                report.skipped += 1

    def _batch_pass(self, graph: ExecutionGraph, report: OptimizationReport) -> None:
        """Group batch-priority leaf tasks together (annotation only)."""
        batch_leaves = [
            node for node in graph.nodes.values()
            if not node.dependents
            and node.requirements.priority == "batch"
            and not node.dependencies
            and not node.metadata.get("merged_into")
        ]
        if len(batch_leaves) > 1:
            batch_group = [n.task_id for n in batch_leaves]
            for node in batch_leaves:
                node.metadata["batch_group"] = batch_group
            report.batched += len(batch_leaves)
