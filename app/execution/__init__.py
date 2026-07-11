"""
AEOS Distributed Execution Engine (DEE) — Phase 8.3

The execution layer sits between the HyperKernel and Agent Runtime.
Compiles raw intent into a validated ExecutionGraph, runs it through
a 15-stage pipeline, and supports distributed parallel execution with
checkpointing, retry, replay, and full observability.

Quick start:
    from app.execution import ExecutionEngine
    result = await engine.run(task="analyze this repo", caller_id="api")

Graph building:
    from app.execution.planner import ExecutionPlanner
    from app.execution.visualization import GraphVisualizer

    planner = ExecutionPlanner(kernel=kernel)
    graph = planner.plan_parallel([
        ("research_agent", "Research topic A"),
        ("analyst_agent",  "Analyze findings"),
    ])
    print(GraphVisualizer().to_mermaid(graph))

Node types:
    from app.execution.graph import AgentNode, ToolNode, ConditionalNode
    from app.execution.node import (
        CapabilityNode, RetryNode, LoopNode,
        ApprovalNode, HumanInputNode, MergeNode,
    )

Retry & resilience:
    from app.execution.retry import RetryPolicy, RetryEngine, CircuitBreaker

Checkpointing:
    from app.execution.checkpoint import InMemoryCheckpointStore

Replay & debugging:
    from app.execution.replay import TraceStore, ReplayEngine

Metrics & observability:
    from app.execution.metrics import MetricsCollector
    from app.execution.events import ExecutionEventBus, ExecutionEventType

Conditions:
    from app.execution.conditions import evaluate_condition, ConditionContext
"""

# ── Core engine ───────────────────────────────────────────────────────────────
from app.execution.engine import ExecutionEngine

# ── Schemas (typed data flow) ─────────────────────────────────────────────────
from app.execution.schemas import (
    RawIntent,
    ValidatedInput,
    ClassifiedIntent,
    TaskType,
    ConstraintSet,
    ConstraintSolution,
    GoalSet,
    Goal,
    GoalType,
    ExecutionPlan,
    ExecutionGraph,
    WorkflowState,
    WorkflowStatus,
    StepResult,
    StepStatus,
    AggregatedResult,
    GovernanceGateResult,
    GovernanceDecision,
    ExecutionError,
)

# ── Graph (node + edge types + compiler + executor) ───────────────────────────
from app.execution.graph import (
    GraphNode,
    AgentNode,
    ToolNode,
    ConditionalNode,
    ParallelNode,
    JoinNode,
    GraphEdge,
    SequentialEdge,
    ConditionalEdge,
    DataFlowEdge,
    GraphCompiler,
    execute_graph,
)

# ── Extended node types (Phase 8.3) ───────────────────────────────────────────
from app.execution.node import (
    CapabilityNode,
    RetryNode,
    LoopNode,
    ApprovalNode,
    HumanInputNode,
    MergeNode,
    NodeFactory,
)

# ── Planner ───────────────────────────────────────────────────────────────────
from app.execution.planner import ExecutionPlanner, PlannerConfig

# ── Retry & resilience ────────────────────────────────────────────────────────
from app.execution.retry import (
    RetryPolicy,
    RetryEngine,
    CircuitBreaker,
    CircuitBreakerState,
    DeadLetterEntry,
    DEFAULT_RETRY_POLICY,
    NO_RETRY_POLICY,
)

# ── Checkpoint ────────────────────────────────────────────────────────────────
from app.execution.checkpoint import (
    Checkpoint,
    CheckpointStore,
    InMemoryCheckpointStore,
)

# ── Worker pool ───────────────────────────────────────────────────────────────
from app.execution.worker import WorkerPool, Worker, WorkerStatus

# ── Node executors ────────────────────────────────────────────────────────────
from app.execution.executor import (
    BaseNodeExecutor,
    AgentNodeExecutor,
    CapabilityNodeExecutor,
    ToolNodeExecutor,
    ConditionalNodeExecutor,
    DispatchingExecutor,
    CompositeExecutor,
)

# ── Events ────────────────────────────────────────────────────────────────────
from app.execution.events import (
    ExecutionEventType,
    ExecutionEvent,
    ExecutionEventBus,
)

# ── Metrics ───────────────────────────────────────────────────────────────────
from app.execution.metrics import (
    NodeMetrics,
    WorkflowMetrics,
    MetricsCollector,
)

# ── Priority & scheduling ─────────────────────────────────────────────────────
from app.execution.priority import PriorityQueue, DeadlineScheduler

# ── Conditions ────────────────────────────────────────────────────────────────
from app.execution.conditions import (
    ConditionContext,
    ConditionEvaluator,
    evaluate_condition,
    ConditionError,
)

# ── Replay & trace ────────────────────────────────────────────────────────────
from app.execution.replay import (
    TraceEntry,
    ExecutionTrace,
    TraceStore,
    ReplayDiff,
    ReplayEngine,
)

# ── Visualization ─────────────────────────────────────────────────────────────
from app.execution.visualization import GraphVisualizer

__all__ = [
    # Engine
    "ExecutionEngine",
    # Schemas
    "RawIntent", "ValidatedInput", "ClassifiedIntent", "TaskType",
    "ConstraintSet", "ConstraintSolution", "GoalSet", "Goal", "GoalType",
    "ExecutionPlan", "ExecutionGraph", "WorkflowState", "WorkflowStatus",
    "StepResult", "StepStatus", "AggregatedResult",
    "GovernanceGateResult", "GovernanceDecision", "ExecutionError",
    # Graph
    "GraphNode", "AgentNode", "ToolNode", "ConditionalNode",
    "ParallelNode", "JoinNode",
    "GraphEdge", "SequentialEdge", "ConditionalEdge", "DataFlowEdge",
    "GraphCompiler", "execute_graph",
    # Extended nodes
    "CapabilityNode", "RetryNode", "LoopNode",
    "ApprovalNode", "HumanInputNode", "MergeNode", "NodeFactory",
    # Planner
    "ExecutionPlanner", "PlannerConfig",
    # Retry
    "RetryPolicy", "RetryEngine", "CircuitBreaker", "CircuitBreakerState",
    "DeadLetterEntry", "DEFAULT_RETRY_POLICY", "NO_RETRY_POLICY",
    # Checkpoint
    "Checkpoint", "CheckpointStore", "InMemoryCheckpointStore",
    # Worker
    "WorkerPool", "Worker", "WorkerStatus",
    # Executors
    "BaseNodeExecutor", "AgentNodeExecutor", "CapabilityNodeExecutor",
    "ToolNodeExecutor", "ConditionalNodeExecutor",
    "DispatchingExecutor", "CompositeExecutor",
    # Events
    "ExecutionEventType", "ExecutionEvent", "ExecutionEventBus",
    # Metrics
    "NodeMetrics", "WorkflowMetrics", "MetricsCollector",
    # Priority
    "PriorityQueue", "DeadlineScheduler",
    # Conditions
    "ConditionContext", "ConditionEvaluator", "evaluate_condition", "ConditionError",
    # Replay
    "TraceEntry", "ExecutionTrace", "TraceStore", "ReplayDiff", "ReplayEngine",
    # Visualization
    "GraphVisualizer",
]
