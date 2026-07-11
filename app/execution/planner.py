"""
AEOS Distributed Execution Engine — Execution Planner

High-level planner that converts structured goals into optimized
ExecutionGraphs. Sits above stage8_plan / stage9_compile_graph and
provides richer graph construction patterns.

Capabilities:
  - plan_from_goals()      — standard goal → graph (wraps stages 8-9)
  - plan_parallel()        — N agents running concurrently → merge
  - plan_pipeline()        — linear chain: A → B → C
  - plan_fan_out_fan_in()  — 1 planner → N workers → 1 merger
  - optimize_parallelism() — detect serial bottlenecks and parallelize
  - critical_path()        — identify the longest path through the graph

The planner is cloud-ready: all graph construction is local, but the
resulting ExecutionGraph can be dispatched to Celery/SQS workers via
adapters without changing this module.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.core.logger import get_logger
from app.execution.graph import (
    AgentNode,
    GraphCompiler,
    GraphNode,
    JoinNode,
    SequentialEdge,
)
from app.execution.node import CapabilityNode, MergeNode, RetryNode, NodeFactory
from app.execution.schemas import (
    ConstraintSolution,
    ExecutionGraph,
    Goal,
    GoalSet,
    GoalType,
)

if TYPE_CHECKING:
    from app.kernel.kernel import AEOSKernel

__all__ = [
    "PlannerConfig",
    "ExecutionPlanner",
]

log = get_logger(__name__)

_GOAL_TYPE_TO_AGENT: dict[GoalType, str] = {
    GoalType.INFORMATION: "research_agent",
    GoalType.EVALUATION:  "analyst_agent",
    GoalType.ACTION:      "executor_agent",
    GoalType.SYNTHESIS:   "simple_agent",
}


@dataclass
class PlannerConfig:
    """Configuration for the ExecutionPlanner."""
    default_agent_type: str = "simple_agent"
    default_timeout_ms: float = 30_000.0
    max_parallel: int = 5
    enable_retry_wrapping: bool = True
    retry_max_attempts: int = 2
    retry_base_delay_ms: float = 500.0
    enable_merge_node: bool = True
    merge_strategy: str = "all"         # "all" | "first" | "best_quality"


class ExecutionPlanner:
    """
    Programmatic execution graph builder.

    Usage:
        planner = ExecutionPlanner(kernel=kernel)

        # Build a parallel research + analysis graph
        graph = planner.plan_fan_out_fan_in(
            fan_out_tasks=[
                ("research_agent", "Research quantum computing trends"),
                ("research_agent", "Research classical computing trends"),
            ],
            merge_task=("analyst_agent", "Compare and synthesize findings"),
        )
    """

    def __init__(
        self,
        kernel: "AEOSKernel | None" = None,
        config: PlannerConfig | None = None,
    ) -> None:
        self._kernel = kernel
        self._config = config or PlannerConfig()
        self._factory = NodeFactory()
        self._compiler = GraphCompiler()

    # ── Goal-based planning (wraps stage 8-9) ─────────────────────────────────

    def plan_from_goals(
        self,
        goal_set: GoalSet,
        solution: ConstraintSolution | None = None,
        trace_id: str = "",
        total_timeout_ms: float = 120_000.0,
    ) -> ExecutionGraph:
        """
        Convert a GoalSet into an ExecutionGraph.

        Replicates and extends stage8_plan + stage9_compile_graph with
        optional retry wrapping and parallelism optimization.
        """
        available_agents = solution.available_agents if solution else [self._config.default_agent_type]
        plan_id = str(uuid.uuid4())

        nodes: list[GraphNode] = []
        edges: list[SequentialEdge] = []
        goal_to_node: dict[str, str] = {}  # goal_id → node_id

        for goal in goal_set.goals:
            agent_type = _GOAL_TYPE_TO_AGENT.get(goal.goal_type, self._config.default_agent_type)
            if available_agents and agent_type not in available_agents:
                agent_type = available_agents[0]

            node: GraphNode = AgentNode(
                goal_id=goal.goal_id,
                agent_type=agent_type,
                task_description=goal.description,
                timeout_ms=max(goal.deadline_ms, 5_000.0),
                priority=goal.priority,
            )

            # Optionally wrap with retry
            if self._config.enable_retry_wrapping and self._config.retry_max_attempts > 1:
                node = RetryNode(
                    inner_node=node,
                    max_retry_attempts=self._config.retry_max_attempts,
                    retry_delay_ms=self._config.retry_base_delay_ms,
                    goal_id=goal.goal_id,
                    priority=goal.priority,
                )

            nodes.append(node)
            goal_to_node[goal.goal_id] = node.node_id

        # Wire dependency edges
        for goal in goal_set.goals:
            for dep_id in goal.dependency_ids:
                from_node = goal_to_node.get(dep_id)
                to_node = goal_to_node.get(goal.goal_id)
                if from_node and to_node:
                    edges.append(SequentialEdge(from_node_id=from_node, to_node_id=to_node))

        graph = self._compiler.compile(
            nodes=nodes,
            edges=edges,
            total_timeout_ms=total_timeout_ms,
            trace_id=trace_id,
            plan_id=plan_id,
        )

        log.info(
            "ExecutionPlanner: plan_from_goals compiled",
            extra={"ctx_nodes": len(nodes), "ctx_edges": len(edges), "ctx_trace_id": trace_id},
        )
        return graph

    # ── Pattern: Linear pipeline ───────────────────────────────────────────────

    def plan_pipeline(
        self,
        steps: list[tuple[str, str]],  # [(agent_type, task_description), ...]
        trace_id: str = "",
        total_timeout_ms: float = 120_000.0,
    ) -> ExecutionGraph:
        """
        Build a strictly sequential pipeline: A → B → C → ...

        Example:
            graph = planner.plan_pipeline([
                ("research_agent",  "Gather market data"),
                ("analyst_agent",   "Analyze the data"),
                ("executor_agent",  "Write the report"),
            ])
        """
        nodes: list[GraphNode] = []
        edges: list[SequentialEdge] = []

        for i, (agent_type, task) in enumerate(steps):
            node = AgentNode(
                agent_type=agent_type,
                task_description=task,
                priority=i + 1,
                timeout_ms=self._config.default_timeout_ms,
            )
            if self._config.enable_retry_wrapping:
                node = RetryNode(
                    inner_node=node,
                    max_retry_attempts=self._config.retry_max_attempts,
                    retry_delay_ms=self._config.retry_base_delay_ms,
                    priority=i + 1,
                )
            if nodes:
                edges.append(SequentialEdge(
                    from_node_id=nodes[-1].node_id,
                    to_node_id=node.node_id,
                ))
            nodes.append(node)

        return self._compiler.compile(
            nodes=nodes,
            edges=edges,
            total_timeout_ms=total_timeout_ms,
            trace_id=trace_id,
        )

    # ── Pattern: Parallel execution ────────────────────────────────────────────

    def plan_parallel(
        self,
        tasks: list[tuple[str, str]],  # [(agent_type, task_description), ...]
        join_strategy: str = "all",
        trace_id: str = "",
        total_timeout_ms: float = 60_000.0,
    ) -> ExecutionGraph:
        """
        Run N tasks concurrently with no dependencies between them.

        Returns an ExecutionGraph where all nodes are at the same depth
        (parallel group), followed by a merge node.

        Example:
            graph = planner.plan_parallel([
                ("research_agent", "Research quantum computing"),
                ("research_agent", "Research AI trends"),
                ("research_agent", "Research cloud adoption"),
            ], join_strategy="all")
        """
        nodes: list[GraphNode] = []
        edges: list[SequentialEdge] = []

        for i, (agent_type, task) in enumerate(tasks):
            node: GraphNode = AgentNode(
                agent_type=agent_type,
                task_description=task,
                priority=3,
                timeout_ms=self._config.default_timeout_ms,
            )
            if self._config.enable_retry_wrapping:
                node = RetryNode(
                    inner_node=node,
                    max_retry_attempts=self._config.retry_max_attempts,
                    retry_delay_ms=self._config.retry_base_delay_ms,
                    priority=3,
                )
            nodes.append(node)

        # Merge node collects all parallel results
        if self._config.enable_merge_node and len(nodes) > 1:
            merge = MergeNode(
                input_node_ids=[n.node_id for n in nodes],
                join_strategy=join_strategy,
                fallback_to_partial=(join_strategy != "all"),
                priority=5,
            )
            nodes.append(merge)
            for worker in nodes[:-1]:
                edges.append(SequentialEdge(from_node_id=worker.node_id, to_node_id=merge.node_id))

        return self._compiler.compile(
            nodes=nodes,
            edges=edges,
            total_timeout_ms=total_timeout_ms,
            trace_id=trace_id,
        )

    # ── Pattern: Fan-out / Fan-in ──────────────────────────────────────────────

    def plan_fan_out_fan_in(
        self,
        planner_task: tuple[str, str] | None,
        fan_out_tasks: list[tuple[str, str]],
        merge_task: tuple[str, str] | None = None,
        join_strategy: str = "all",
        trace_id: str = "",
        total_timeout_ms: float = 120_000.0,
    ) -> ExecutionGraph:
        """
        Classic map-reduce pattern:

            [Planner] → [Worker1, Worker2, Worker3] → [Merger]

        planner_task and merge_task are optional.

        Example:
            graph = planner.plan_fan_out_fan_in(
                planner_task=("planner_agent", "Break down the research task"),
                fan_out_tasks=[
                    ("research_agent", "Research topic A"),
                    ("research_agent", "Research topic B"),
                ],
                merge_task=("analyst_agent", "Synthesize all research"),
            )
        """
        nodes: list[GraphNode] = []
        edges: list[SequentialEdge] = []

        # Optional planner node
        planner_node: GraphNode | None = None
        if planner_task:
            agent_type, task = planner_task
            planner_node = AgentNode(
                agent_type=agent_type,
                task_description=task,
                priority=1,
                timeout_ms=self._config.default_timeout_ms,
            )
            nodes.append(planner_node)

        # Fan-out worker nodes
        worker_nodes: list[GraphNode] = []
        for i, (agent_type, task) in enumerate(fan_out_tasks):
            worker: GraphNode = AgentNode(
                agent_type=agent_type,
                task_description=task,
                priority=3,
                timeout_ms=self._config.default_timeout_ms,
            )
            if self._config.enable_retry_wrapping:
                worker = RetryNode(
                    inner_node=worker,
                    max_retry_attempts=self._config.retry_max_attempts,
                    retry_delay_ms=self._config.retry_base_delay_ms,
                    priority=3,
                )
            worker_nodes.append(worker)
            nodes.append(worker)
            if planner_node:
                edges.append(SequentialEdge(
                    from_node_id=planner_node.node_id,
                    to_node_id=worker.node_id,
                ))

        # Merge node
        merge_bridge: MergeNode | None = None
        if len(worker_nodes) > 1:
            merge_bridge = MergeNode(
                input_node_ids=[w.node_id for w in worker_nodes],
                join_strategy=join_strategy,
                fallback_to_partial=(join_strategy != "all"),
                priority=5,
            )
            nodes.append(merge_bridge)
            for worker in worker_nodes:
                edges.append(SequentialEdge(
                    from_node_id=worker.node_id,
                    to_node_id=merge_bridge.node_id,
                ))

        # Optional final synthesis node
        if merge_task:
            agent_type, task = merge_task
            synth_node: GraphNode = AgentNode(
                agent_type=agent_type,
                task_description=task,
                priority=7,
                timeout_ms=self._config.default_timeout_ms,
            )
            nodes.append(synth_node)
            bridge_id = merge_bridge.node_id if merge_bridge else (
                worker_nodes[-1].node_id if worker_nodes else None
            )
            if bridge_id:
                edges.append(SequentialEdge(from_node_id=bridge_id, to_node_id=synth_node.node_id))

        return self._compiler.compile(
            nodes=nodes,
            edges=edges,
            total_timeout_ms=total_timeout_ms,
            trace_id=trace_id,
        )

    # ── Graph Analysis ────────────────────────────────────────────────────────

    def critical_path(self, graph: ExecutionGraph) -> list[str]:
        """
        Identify the critical path (longest path by node count) through the graph.

        Returns a list of node_ids representing the longest sequential chain.
        This is the bottleneck that determines minimum execution time.
        """
        if not graph.topological_order:
            return []

        # Build adjacency from edges
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            if edge.edge_type == "sequential":
                adjacency[edge.from_node_id].append(edge.to_node_id)

        # DP: longest path from each node
        memo: dict[str, list[str]] = {}

        def longest_from(node_id: str) -> list[str]:
            if node_id in memo:
                return memo[node_id]
            best: list[str] = [node_id]
            for child in adjacency[node_id]:
                candidate = [node_id] + longest_from(child)
                if len(candidate) > len(best):
                    best = candidate
            memo[node_id] = best
            return best

        # Find roots (no incoming sequential edges)
        has_incoming: set[str] = set()
        for edge in graph.edges:
            if edge.edge_type == "sequential":
                has_incoming.add(edge.to_node_id)
        roots = [nid for nid in graph.topological_order if nid not in has_incoming]

        overall_best: list[str] = []
        for root in roots:
            path = longest_from(root)
            if len(path) > len(overall_best):
                overall_best = path

        return overall_best

    def optimize_parallelism(self, graph: ExecutionGraph) -> ExecutionGraph:
        """
        Analyze the graph and return an optimization report.

        Currently returns the graph unchanged — future implementations
        will reorder node priorities to maximize parallel batch sizes.
        The method is a hook for future optimization passes.
        """
        critical = self.critical_path(graph)
        parallel_groups = graph.parallel_groups
        log.info(
            "ExecutionPlanner: graph analysis",
            extra={
                "ctx_graph_id": graph.graph_id,
                "ctx_nodes": len(graph.nodes),
                "ctx_critical_path_len": len(critical),
                "ctx_parallel_groups": len(parallel_groups),
            },
        )
        return graph  # Currently a no-op; future: reorder for better batching

    def summarize_graph(self, graph: ExecutionGraph) -> dict[str, Any]:
        """Return a human-readable summary of graph structure."""
        critical = self.critical_path(graph)
        node_types: dict[str, int] = defaultdict(int)
        for node in graph.nodes:
            node_types[node.node_type] += 1
        return {
            "graph_id": graph.graph_id,
            "node_count": len(graph.nodes),
            "edge_count": len(graph.edges),
            "parallel_groups": len(graph.parallel_groups),
            "topological_depth": len(graph.topological_order),
            "critical_path_length": len(critical),
            "critical_path": critical,
            "node_types": dict(node_types),
            "estimated_min_latency_ms": (
                len(critical) * self._config.default_timeout_ms * 0.3  # rough estimate
            ),
        }
