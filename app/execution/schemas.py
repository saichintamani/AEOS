"""
AEOS Execution Engine — Typed Data Structures

All typed data structures produced and consumed by the 15-stage execution
pipeline. Defined from the spec in docs/architecture/006-EXECUTION_ENGINE.md.

Every data structure is a Python dataclass. All are JSON-serializable.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Stage 1 ────────────────────────────────────────────────────────────────────

@dataclass
class RawIntent:
    """Output of Stage 1: Intent Reception."""
    raw_task: str
    mode: str                          # "auto" | "plan_only" | "dry_run" | "sync"
    caller_id: str
    request_id: str
    trace_id: str = field(default_factory=_uuid)
    received_at: str = field(default_factory=_now)


# ── Stage 2 ────────────────────────────────────────────────────────────────────

@dataclass
class ValidatedInput:
    """Output of Stage 2: Input Validation."""
    task: str
    mode: str
    caller_id: str
    trace_id: str
    policy_warnings: list[str] = field(default_factory=list)
    validated_at: str = field(default_factory=_now)


# ── Stage 3 ────────────────────────────────────────────────────────────────────

class TaskType(str, Enum):
    RESEARCH       = "RESEARCH"
    ANALYSIS       = "ANALYSIS"
    EXECUTION      = "EXECUTION"
    PLANNING       = "PLANNING"
    SYNTHESIS      = "SYNTHESIS"
    CONVERSATIONAL = "CONVERSATIONAL"
    MULTI_STEP     = "MULTI_STEP"
    UNKNOWN        = "UNKNOWN"


@dataclass
class Entity:
    entity_type: str     # "url" | "file_path" | "code" | "concept" | "date" | "quantity"
    value: str
    span: tuple[int, int] = field(default_factory=lambda: (0, 0))


@dataclass
class ClassifiedIntent:
    """Output of Stage 3: Intent Understanding."""
    task: str
    task_type: TaskType
    mode: str = "auto"                 # "auto" | "plan_only" | "dry_run" | "sync" | "multi-agent"
    entities: list[Entity] = field(default_factory=list)
    needs_clarification: bool = False
    ambiguity_score: float = 0.0
    complexity: str = "simple"         # "simple" | "medium" | "complex"
    estimated_steps: int = 1
    output_format: str = "markdown"    # "markdown" | "json" | "plain" | "structured"
    trace_id: str = ""
    policy_warnings: list[str] = field(default_factory=list)


# ── Stage 4 ────────────────────────────────────────────────────────────────────

@dataclass
class ConstraintSet:
    """Output of Stage 4: Constraint Collection."""
    max_steps: int = 10
    max_parallel_agents: int = 3
    timeout_seconds: float = 120.0
    max_cost_tokens: int = 100_000
    max_cost_usd: float = 1.0
    allowed_tool_categories: list[str] = field(default_factory=lambda: ["search", "code", "file"])
    blocked_tool_categories: list[str] = field(default_factory=list)
    require_reviewer: bool = False
    require_reflection: bool = True
    min_quality_threshold: float = 0.75
    mode_overrides: dict[str, Any] = field(default_factory=dict)
    caller_tier: str = "standard"
    warnings: list[str] = field(default_factory=list)


# ── Stage 5 ────────────────────────────────────────────────────────────────────

@dataclass
class ConstraintDegradation:
    constraint: str
    original_value: Any
    degraded_value: Any
    reason: str


@dataclass
class ConstraintSolution:
    """Output of Stage 5: Constraint Solving."""
    satisfiable: bool
    available_agents: list[str] = field(default_factory=list)
    available_tools: list[str] = field(default_factory=list)
    effective_timeout_seconds: float = 120.0
    effective_max_parallel: int = 3
    degradations: list[ConstraintDegradation] = field(default_factory=list)
    unsatisfied_hard_constraints: list[str] = field(default_factory=list)


# ── Stages 6-7 ─────────────────────────────────────────────────────────────────

class GoalType(str, Enum):
    INFORMATION = "INFORMATION"
    ACTION      = "ACTION"
    SYNTHESIS   = "SYNTHESIS"
    EVALUATION  = "EVALUATION"


@dataclass
class SuccessCriteria:
    min_word_count: int = 0
    required_entities: list[str] = field(default_factory=list)
    required_format: str = ""
    evaluation_metrics: list[str] = field(default_factory=lambda: ["COMPLETENESS", "RELEVANCE"])


@dataclass
class Goal:
    goal_id: str = field(default_factory=_uuid)
    description: str = ""
    goal_type: GoalType = GoalType.INFORMATION
    priority: int = 3
    dependency_ids: list[str] = field(default_factory=list)
    deadline_ms: float = 30_000.0
    success_criteria: SuccessCriteria = field(default_factory=SuccessCriteria)
    fallback_description: str = ""
    resource_budget_tokens: int = 10_000


@dataclass
class GoalSet:
    goals: list[Goal] = field(default_factory=list)
    decomposition_strategy: str = "sequential"

    def by_priority(self) -> list[Goal]:
        return sorted(self.goals, key=lambda g: g.priority)


# ── Stage 8 ────────────────────────────────────────────────────────────────────

@dataclass
class AgentAssignment:
    goal_id: str
    primary_agent_type: str
    fallback_agent_types: list[str] = field(default_factory=list)
    tool_ids: list[str] = field(default_factory=list)


@dataclass
class ExecutionPlan:
    """Output of Stage 8: Planning."""
    plan_id: str = field(default_factory=_uuid)
    assignments: list[AgentAssignment] = field(default_factory=list)
    goal_set: GoalSet = field(default_factory=GoalSet)
    execution_strategy: str = "topological"
    trace_id: str = ""


# ── Stage 9 ────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionGraph:
    """Output of Stage 9: Graph Compilation. Immutable after compilation."""
    graph_id: str = field(default_factory=_uuid)
    nodes: list[Any] = field(default_factory=list)
    edges: list[Any] = field(default_factory=list)
    parallel_groups: list[list[str]] = field(default_factory=list)
    topological_order: list[str] = field(default_factory=list)
    compiled_at: str = field(default_factory=_now)
    total_timeout_ms: float = 120_000.0
    trace_id: str = ""
    plan_id: str = ""


# ── Stage 10 ───────────────────────────────────────────────────────────────────

class WorkflowStatus(str, Enum):
    PENDING      = "PENDING"
    PLANNING     = "PLANNING"
    EXECUTING    = "EXECUTING"
    REFLECTING   = "REFLECTING"
    AGGREGATING  = "AGGREGATING"
    GOVERNANCE   = "GOVERNANCE"
    COMPLETED    = "COMPLETED"
    FAILED       = "FAILED"


@dataclass
class WorkflowState:
    """Live state of a running workflow. Mutable during Stages 10-15."""
    workflow_id: str = field(default_factory=_uuid)
    status: WorkflowStatus = WorkflowStatus.PENDING
    execution_graph: Optional[ExecutionGraph] = None
    step_results: dict[str, Any] = field(default_factory=dict)
    completed_nodes: set[str] = field(default_factory=set)
    failed_nodes: set[str] = field(default_factory=set)
    skipped_nodes: set[str] = field(default_factory=set)
    started_at: str = field(default_factory=_now)
    revision_count: int = 0
    revision_history: list[Any] = field(default_factory=list)
    trace_id: str = ""
    task_id: str = ""


# ── Stages 11-12 ───────────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    COMPLETED  = "COMPLETED"
    FAILED     = "FAILED"
    TIMED_OUT  = "TIMED_OUT"
    SKIPPED    = "SKIPPED"


@dataclass
class StepResult:
    node_id: str
    status: StepStatus
    value: Any = None
    error: str = ""
    agent_id: str = ""
    latency_ms: float = 0.0
    token_cost: int = 0
    confidence: float = 1.0
    produced_at: str = field(default_factory=_now)


# ── Stage 13 ───────────────────────────────────────────────────────────────────

@dataclass
class AggregatedResult:
    content: str
    content_format: str = "markdown"
    supporting_data: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, str] = field(default_factory=dict)
    partial: bool = False
    partial_success_ratio: float = 1.0
    total_token_cost: int = 0
    total_latency_ms: float = 0.0
    agents_used: list[str] = field(default_factory=list)
    confidence: float = 1.0


# ── Stage 14 ───────────────────────────────────────────────────────────────────

class ReflectionDecision(str, Enum):
    PASS         = "PASS"
    REVISE       = "REVISE"
    PASS_WARNED  = "PASS_WARNED"


@dataclass
class GoalEvaluation:
    goal_id: str
    quality_score: float
    completeness: float = 1.0
    relevance: float = 1.0
    coherence: float = 1.0
    factuality: Optional[float] = None
    format_ok: bool = True
    issues: list[str] = field(default_factory=list)


@dataclass
class ReflectionGateResult:
    decision: ReflectionDecision
    overall_quality_score: float
    goal_evaluations: list[GoalEvaluation] = field(default_factory=list)
    revision_targets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class RevisionRecord:
    revision_number: int
    target_node_ids: list[str]
    pre_revision_quality: float
    post_revision_quality: float
    triggered_at: str = field(default_factory=_now)


# ── Stage 15 ───────────────────────────────────────────────────────────────────

class GovernanceDecision(str, Enum):
    PASS    = "PASS"
    REDACT  = "REDACT"
    BLOCK   = "BLOCK"


@dataclass
class CostBreakdown:
    token_cost_tokens: int = 0
    token_cost_usd: float = 0.0
    tool_cost_usd: float = 0.0
    compute_cost_usd: float = 0.0
    total_usd: float = 0.0


@dataclass
class AuditRecord:
    trace_id: str
    task_id: str
    caller_id: str
    task_preview: str
    agents_used: list[str]
    tools_used: list[str]
    stage_latencies: dict[str, float]
    total_cost: CostBreakdown
    quality_score: float
    governance_decision: GovernanceDecision
    timestamp: str = field(default_factory=_now)


@dataclass
class GovernanceGateResult:
    governance_decision: GovernanceDecision
    status: str                          # "completed" | "completed_partial" | "failed" | "blocked"
    result: Any
    thought: str
    trace_id: str = ""
    quality_score: float = 1.0
    latency_ms: float = 0.0
    cost: CostBreakdown = field(default_factory=CostBreakdown)
    warnings: list[str] = field(default_factory=list)
    agents_used: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "result": self.result,
            "thought": self.thought,
            "trace_id": self.trace_id,
            "quality_score": self.quality_score,
            "latency_ms": self.latency_ms,
            "governance_decision": self.governance_decision.value,
            "warnings": self.warnings,
            "agents_used": self.agents_used,
            "cost": {
                "token_cost_tokens": self.cost.token_cost_tokens,
                "total_usd": self.cost.total_usd,
            },
            "timestamp": self.timestamp,
        }


# ── Execution Error ─────────────────────────────────────────────────────────────

@dataclass
class ExecutionError:
    """Typed error returned when the pipeline cannot complete."""
    code: str
    message: str
    trace_id: str = ""
    stage: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_governance_result(self) -> GovernanceGateResult:
        return GovernanceGateResult(
            governance_decision=GovernanceDecision.BLOCK,
            status="failed",
            result=None,
            thought=f"Pipeline error at stage {self.stage}: {self.message}",
            trace_id=self.trace_id,
            quality_score=0.0,
            warnings=[f"{self.code}: {self.message}"],
        )
