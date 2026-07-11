"""
Wave 9B.4.2 — Workflow Compiler

Compiles an incoming workflow definition into a validated, annotated
ExecutionGraph ready for the Execution Planner.

Steps:
  1. Parse WorkflowDefinition (task specs + explicit edges)
  2. Validate: detect cycles, orphan tasks, duplicate IDs
  3. Annotate with governance tags, resource estimates, priority
  4. Emit WORKFLOW_COMPILED telemetry

WorkflowDefinition — raw input structure
WorkflowCompiler   — validates and compiles to ExecutionGraph
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.runtime_intelligence.contracts import (
    ExecutionGraph,
    TaskDependencyType,
    TaskNode,
    TaskRequirements,
)
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


@dataclass
class TaskSpec:
    task_id: str
    task_type: str
    priority: str = "normal"
    depends_on: list[str] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    required_models: list[str] = field(default_factory=list)
    requires_gpu: bool = False
    required_memory_gb: float = 0.0
    max_latency_ms: float = float("inf")
    max_cost: float = float("inf")
    payload: dict[str, Any] = field(default_factory=dict)
    estimated_duration_ms: float = 0.0
    can_parallelize: bool = True


@dataclass
class WorkflowDefinition:
    workflow_id: str
    name: str = ""
    tasks: list[TaskSpec] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class CompilationError(ValueError):
    """Raised when a WorkflowDefinition cannot be compiled."""


class WorkflowCompiler:
    """
    Compiles a WorkflowDefinition into a validated ExecutionGraph.

    Validates:
      - No duplicate task IDs
      - All depends_on references exist
      - No dependency cycles (topological sort)
    """

    def __init__(self, telemetry_bus: TelemetryBus | None = None) -> None:
        self._bus = telemetry_bus

    def compile(self, definition: WorkflowDefinition) -> ExecutionGraph:
        self._validate(definition)
        graph = self._build_graph(definition)
        logger.info(
            "WorkflowCompiler: compiled workflow '%s' → %d nodes, %d edges",
            definition.workflow_id, len(graph.nodes), len(graph.edges),
        )
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.TASK_SUBMITTED,
                source="WorkflowCompiler",
                payload={
                    "workflow_id": definition.workflow_id,
                    "task_count": len(graph.nodes),
                },
                correlation_id=definition.workflow_id,
            ))
        return graph

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate(self, defn: WorkflowDefinition) -> None:
        ids = [t.task_id for t in defn.tasks]

        # Duplicate IDs
        seen: set[str] = set()
        for tid in ids:
            if tid in seen:
                raise CompilationError(f"Duplicate task_id: {tid!r}")
            seen.add(tid)

        # Unknown depends_on
        id_set = set(ids)
        for task in defn.tasks:
            for dep in task.depends_on:
                if dep not in id_set:
                    raise CompilationError(
                        f"Task {task.task_id!r} depends_on unknown task {dep!r}"
                    )

        # Cycle detection via DFS
        adj: dict[str, list[str]] = {t.task_id: t.depends_on for t in defn.tasks}
        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node: str) -> None:
            visited.add(node)
            in_stack.add(node)
            for dep in adj.get(node, []):
                if dep not in visited:
                    dfs(dep)
                elif dep in in_stack:
                    raise CompilationError(
                        f"Dependency cycle detected involving task {dep!r}"
                    )
            in_stack.discard(node)

        for tid in ids:
            if tid not in visited:
                dfs(tid)

    def _build_graph(self, defn: WorkflowDefinition) -> ExecutionGraph:
        graph = ExecutionGraph(workflow_id=defn.workflow_id)

        for spec in defn.tasks:
            req = TaskRequirements(
                task_id=spec.task_id,
                task_type=spec.task_type,
                workflow_id=defn.workflow_id,
                priority=spec.priority,
                required_skills=frozenset(spec.required_skills),
                required_models=spec.required_models,
                requires_gpu=spec.requires_gpu,
                required_memory_gb=spec.required_memory_gb,
                max_latency_ms=spec.max_latency_ms,
                max_cost=spec.max_cost,
                payload=spec.payload.copy(),
            )
            node = TaskNode(
                task_id=spec.task_id,
                task_type=spec.task_type,
                requirements=req,
                can_parallelize=spec.can_parallelize,
                estimated_duration_ms=spec.estimated_duration_ms,
                priority={"critical": 4, "high": 3, "normal": 2, "low": 1, "batch": 0}.get(
                    spec.priority, 2
                ),
            )
            graph.add_node(node)

        for spec in defn.tasks:
            for dep_id in spec.depends_on:
                graph.add_edge(dep_id, spec.task_id, TaskDependencyType.SEQUENTIAL)

        return graph
