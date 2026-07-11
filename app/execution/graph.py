"""
AEOS Execution Engine — Execution Graph

Node types, edge types, and the topological executor.
Implements Kahn's algorithm with priority-sorted parallel batching.

See docs/architecture/006-EXECUTION_ENGINE.md §6.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger
from app.execution.schemas import (
    ExecutionGraph,
    StepResult,
    StepStatus,
    WorkflowState,
)

__all__ = [
    "GraphNode",
    "AgentNode",
    "ToolNode",
    "ConditionalNode",
    "ParallelNode",
    "JoinNode",
    "GraphEdge",
    "SequentialEdge",
    "ConditionalEdge",
    "DataFlowEdge",
    "GraphCompiler",
    "execute_graph",
]

log = get_logger(__name__)


# ── Node Types ─────────────────────────────────────────────────────────────────

@dataclass
class GraphNode:
    """Base class for all execution graph nodes."""
    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    node_type: str = "base"
    goal_id: str = ""
    timeout_ms: float = 30_000.0
    retry_count: int = 0
    max_retries: int = 1
    priority: int = 3


@dataclass
class AgentNode(GraphNode):
    """Dispatches a task to a specific agent."""
    node_type: str = "agent"
    agent_type: str = ""
    agent_instance_id: str = ""
    task_description: str = ""
    context_keys: list[str] = field(default_factory=list)
    output_key: str = ""


@dataclass
class ToolNode(GraphNode):
    """Invokes a registered tool directly."""
    node_type: str = "tool"
    tool_id: str = ""
    tool_params: dict[str, Any] = field(default_factory=dict)
    output_key: str = ""


@dataclass
class ConditionalNode(GraphNode):
    """Evaluates a condition; enables/disables downstream nodes."""
    node_type: str = "conditional"
    condition_expr: str = ""
    true_branch_node_ids: list[str] = field(default_factory=list)
    false_branch_node_ids: list[str] = field(default_factory=list)


@dataclass
class ParallelNode(GraphNode):
    """Marks children for concurrent execution."""
    node_type: str = "parallel"
    child_node_ids: list[str] = field(default_factory=list)


@dataclass
class JoinNode(GraphNode):
    """Waits for multiple upstream nodes; merges results."""
    node_type: str = "join"
    input_node_ids: list[str] = field(default_factory=list)
    join_strategy: str = "all"     # "all" | "first" | "majority" | "best_quality"
    output_key: str = ""


# ── Edge Types ─────────────────────────────────────────────────────────────────

@dataclass
class GraphEdge:
    edge_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    from_node_id: str = ""
    to_node_id: str = ""
    edge_type: str = "sequential"


@dataclass
class SequentialEdge(GraphEdge):
    """from_node must COMPLETE before to_node can start."""
    edge_type: str = "sequential"


@dataclass
class ConditionalEdge(GraphEdge):
    """to_node starts only if condition evaluates to the given bool."""
    edge_type: str = "conditional"
    condition_value: bool = True


@dataclass
class DataFlowEdge(GraphEdge):
    """Carries specific data keys from from_node to to_node."""
    edge_type: str = "data_flow"
    data_keys: list[str] = field(default_factory=list)


# ── Graph Compiler ─────────────────────────────────────────────────────────────

class GraphCompiler:
    """
    Validates an ExecutionPlan and produces an immutable ExecutionGraph.

    Stage 9 of the execution pipeline.
    """

    def compile(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        total_timeout_ms: float = 120_000.0,
        trace_id: str = "",
        plan_id: str = "",
    ) -> ExecutionGraph:
        """
        Validate and compile a list of nodes and edges into an ExecutionGraph.

        Raises:
            ValueError: if the graph has cycles or type mismatches.
        """
        node_ids = {n.node_id for n in nodes}

        # Validate edge references
        for edge in edges:
            if edge.from_node_id not in node_ids:
                raise ValueError(f"Edge references unknown from_node_id: {edge.from_node_id}")
            if edge.to_node_id not in node_ids:
                raise ValueError(f"Edge references unknown to_node_id: {edge.to_node_id}")

        # Build in-degree map for topological sort
        in_degree: dict[str, int] = {n.node_id: 0 for n in nodes}
        adjacency: dict[str, list[str]] = defaultdict(list)

        for edge in edges:
            if isinstance(edge, SequentialEdge):
                in_degree[edge.to_node_id] += 1
                adjacency[edge.from_node_id].append(edge.to_node_id)

        # Topological sort (Kahn's algorithm)
        queue: deque[str] = deque(nid for nid, deg in in_degree.items() if deg == 0)
        topo_order: list[str] = []

        in_degree_copy = dict(in_degree)
        while queue:
            nid = queue.popleft()
            topo_order.append(nid)
            for downstream in adjacency[nid]:
                in_degree_copy[downstream] -= 1
                if in_degree_copy[downstream] == 0:
                    queue.append(downstream)

        if len(topo_order) != len(nodes):
            raise ValueError("ExecutionGraph has a cycle — compilation aborted.")

        # Identify parallel groups: nodes with no sequential edges between them
        parallel_groups = self._find_parallel_groups(nodes, edges, topo_order)

        return ExecutionGraph(
            nodes=nodes,
            edges=edges,
            parallel_groups=parallel_groups,
            topological_order=topo_order,
            total_timeout_ms=total_timeout_ms,
            trace_id=trace_id,
            plan_id=plan_id,
        )

    def _find_parallel_groups(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        topo_order: list[str],
    ) -> list[list[str]]:
        """Group nodes that can run in parallel (no path between them)."""
        # Simple approach: nodes at the same "level" (same depth from roots)
        adjacency: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            if isinstance(edge, SequentialEdge):
                adjacency[edge.from_node_id].append(edge.to_node_id)

        in_degree: dict[str, int] = {n.node_id: 0 for n in nodes}
        for edge in edges:
            if isinstance(edge, SequentialEdge):
                in_degree[edge.to_node_id] += 1

        depth: dict[str, int] = {}
        roots = [nid for nid, deg in in_degree.items() if deg == 0]
        for nid in roots:
            depth[nid] = 0

        visited: set[str] = set()
        bfs: deque[str] = deque(roots)
        while bfs:
            nid = bfs.popleft()
            if nid in visited:
                continue
            visited.add(nid)
            for dn in adjacency[nid]:
                depth[dn] = max(depth.get(dn, 0), depth.get(nid, 0) + 1)
                bfs.append(dn)

        # Group by depth
        by_depth: dict[int, list[str]] = defaultdict(list)
        for nid, d in depth.items():
            by_depth[d].append(nid)

        return [group for group in by_depth.values() if len(group) > 1]


# ── Topological Executor ───────────────────────────────────────────────────────

NodeExecutor = Callable[[GraphNode, WorkflowState], Coroutine[Any, Any, StepResult]]


async def execute_graph(
    graph: ExecutionGraph,
    workflow_state: WorkflowState,
    execute_node: NodeExecutor,
    max_parallel: int = 3,
) -> WorkflowState:
    """
    Kahn's algorithm with priority-sorted parallel batch execution.

    Dispatches up to max_parallel ready nodes concurrently.
    Updates WorkflowState in-place with step results.
    """
    node_map: dict[str, GraphNode] = {n.node_id: n for n in graph.nodes}

    # Build in-degree and adjacency from sequential edges only
    in_degree: dict[str, int] = {n.node_id: 0 for n in graph.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)

    for edge in graph.edges:
        if edge.edge_type == "sequential":
            in_degree[edge.to_node_id] += 1
            adjacency[edge.from_node_id].append(edge.to_node_id)

    # Initialize ready queue (nodes with no dependencies), sorted by priority
    ready: deque[GraphNode] = deque(
        sorted(
            [node_map[nid] for nid, deg in in_degree.items() if deg == 0],
            key=lambda n: n.priority,
        )
    )

    while ready:
        # Extract batch
        batch: list[GraphNode] = []
        while ready and len(batch) < max_parallel:
            batch.append(ready.popleft())

        # Skip any nodes that were already skipped (upstream failure)
        active_batch = [n for n in batch if n.node_id not in workflow_state.skipped_nodes]

        if not active_batch:
            # Unblock downstream for skipped nodes
            for node in batch:
                for downstream_id in adjacency[node.node_id]:
                    in_degree[downstream_id] -= 1
                    if in_degree[downstream_id] == 0:
                        _insert_priority(ready, node_map[downstream_id])
            continue

        # Execute batch concurrently
        results = await asyncio.gather(
            *[_execute_with_timeout(node, workflow_state, execute_node) for node in active_batch],
            return_exceptions=True,
        )

        # Process results
        for node, result in zip(active_batch, results):
            if isinstance(result, Exception):
                step_result = StepResult(
                    node_id=node.node_id,
                    status=StepStatus.FAILED,
                    error=str(result),
                )
            else:
                step_result = result

            workflow_state.step_results[node.node_id] = step_result

            if step_result.status == StepStatus.COMPLETED:
                workflow_state.completed_nodes.add(node.node_id)
            else:
                workflow_state.failed_nodes.add(node.node_id)

            # Unblock downstream nodes
            for downstream_id in adjacency[node.node_id]:
                in_degree[downstream_id] -= 1
                if in_degree[downstream_id] == 0:
                    downstream_node = node_map[downstream_id]
                    # If this node failed and downstream requires it, skip downstream
                    if (step_result.status != StepStatus.COMPLETED
                            and downstream_node.priority <= 2):
                        workflow_state.skipped_nodes.add(downstream_id)
                    else:
                        _insert_priority(ready, downstream_node)

        # Handle skipped-only nodes in original batch
        for node in batch:
            if node not in active_batch:
                for downstream_id in adjacency[node.node_id]:
                    in_degree[downstream_id] -= 1
                    if in_degree[downstream_id] == 0:
                        _insert_priority(ready, node_map[downstream_id])

    return workflow_state


async def _execute_with_timeout(
    node: GraphNode,
    workflow_state: WorkflowState,
    execute_node: NodeExecutor,
) -> StepResult:
    try:
        return await asyncio.wait_for(
            execute_node(node, workflow_state),
            timeout=node.timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        return StepResult(
            node_id=node.node_id,
            status=StepStatus.TIMED_OUT,
            error=f"Node timed out after {node.timeout_ms}ms",
        )


def _insert_priority(queue: deque[GraphNode], node: GraphNode) -> None:
    """Insert node maintaining priority order (lower = higher priority)."""
    for i, existing in enumerate(queue):
        if node.priority < existing.priority:
            # Rebuild deque with node inserted at position i
            items = list(queue)
            items.insert(i, node)
            queue.clear()
            queue.extend(items)
            return
    queue.append(node)
