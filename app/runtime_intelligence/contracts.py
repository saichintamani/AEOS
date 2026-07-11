"""
Intelligent Orchestration Core (IOC) — shared contracts and data models.

Phase 9B.3: AEOS stops being "distributed infrastructure" and becomes an
intelligent AI orchestration platform.

All IOC modules operate on these canonical types. Nothing in this file
imports from implementation modules — only stdlib and dataclasses.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


# ── Capability Profile (9B.3.1) ───────────────────────────────────────────────

@dataclass
class CapabilityProfile:
    """
    Rich worker capability advertisement.

    Workers publish this profile on join and update it on heartbeat.
    The CapabilityGraph indexes it for fast matching.
    """
    worker_id: str

    # Compute resources
    memory_gb: float = 0.0
    gpu_available: bool = False
    gpu_memory_gb: float = 0.0
    cpu_cores: int = 1

    # AI model support
    supported_models: list[str] = field(default_factory=list)
    supported_agents: list[str] = field(default_factory=list)

    # Performance characteristics
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    throughput_rps: float = 0.0

    # Cost
    token_cost_per_k: float = 0.0
    compute_cost_per_hour: float = 0.0

    # Health & trust
    health_score: float = 1.0            # 0–1
    trust_score: float = 1.0            # 0–1 (governance-assigned)
    historical_success_rate: float = 1.0 # 0–1 (from learning engine)

    # Functional capabilities
    skills: frozenset[str] = field(default_factory=frozenset)
    version: str = "0.0.0"
    dependencies: list[str] = field(default_factory=list)

    # Live state
    current_load: float = 0.0           # 0–1
    queue_depth: int = 0
    region: str = "us-east-1"
    az: str = "a"

    updated_at: str = field(default_factory=_now_iso)

    @property
    def is_healthy(self) -> bool:
        return self.health_score >= 0.5

    @property
    def effective_score(self) -> float:
        """Composite readiness score for scheduling decisions."""
        return (
            0.3 * self.historical_success_rate
            + 0.25 * self.health_score
            + 0.25 * self.trust_score
            + 0.2 * (1.0 - self.current_load)
        )


@dataclass
class TaskRequirements:
    """What a task needs from the worker that executes it."""
    task_type: str = ""
    task_id: str = field(default_factory=_new_id)

    # Resource requirements
    required_memory_gb: float = 0.0
    requires_gpu: bool = False
    required_gpu_memory_gb: float = 0.0

    # AI requirements
    required_models: list[str] = field(default_factory=list)
    required_agents: list[str] = field(default_factory=list)
    required_skills: frozenset[str] = field(default_factory=frozenset)

    # Performance constraints
    max_latency_ms: float = float("inf")
    max_cost: float = float("inf")
    priority: str = "normal"
    deadline: datetime | None = None

    # Placement preferences
    preferred_region: str = ""
    preferred_az: str = ""
    affinity_worker_id: str = ""
    anti_affinity_worker_id: str = ""

    # Metadata
    workflow_id: str = ""
    step_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


# ── Capability Scoring (9B.3.1) ───────────────────────────────────────────────

@dataclass
class CapabilityScore:
    """Scored match between a worker and a task requirement."""
    worker_id: str
    task_id: str
    total_score: float           # 0–1, higher is better
    resource_score: float        # GPU/memory/CPU fit
    latency_score: float         # latency constraint fit
    cost_score: float            # cost constraint fit
    trust_score: float           # governance trust
    capability_score: float      # skill/model/agent match
    load_score: float            # current load penalty
    locality_score: float        # region/AZ affinity
    explanation: str = ""

    @property
    def is_eligible(self) -> bool:
        return (
            self.resource_score > 0.0
            and self.capability_score > 0.0
            and self.total_score > 0.0
        )


# ── DAG / Execution Graph (9B.3.2) ────────────────────────────────────────────

class TaskDependencyType(str, Enum):
    SEQUENTIAL  = "sequential"   # B runs after A completes
    PARALLEL    = "parallel"     # A and B run concurrently
    CONDITIONAL = "conditional"  # B runs only if A succeeds
    MERGE       = "merge"        # C waits for both A and B


@dataclass
class TaskNode:
    """Node in the execution DAG."""
    task_id: str = field(default_factory=_new_id)
    task_type: str = ""
    requirements: TaskRequirements = field(default_factory=TaskRequirements)
    dependencies: list[str] = field(default_factory=list)   # task_ids that must precede this
    dependents: list[str] = field(default_factory=list)      # task_ids that depend on this
    can_parallelize: bool = True
    estimated_duration_ms: float = 0.0
    priority: int = 0   # higher = more important
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionGraph:
    """Complete execution plan as a DAG."""
    graph_id: str = field(default_factory=_new_id)
    workflow_id: str = ""
    nodes: dict[str, TaskNode] = field(default_factory=dict)
    edges: list[tuple[str, str, TaskDependencyType]] = field(default_factory=list)

    # Computed by planners
    parallel_groups: list[list[str]] = field(default_factory=list)
    critical_path: list[str] = field(default_factory=list)
    estimated_total_duration_ms: float = 0.0
    estimated_total_cost: float = 0.0

    created_at: str = field(default_factory=_now_iso)

    def add_node(self, node: TaskNode) -> None:
        self.nodes[node.task_id] = node

    def add_edge(
        self,
        from_id: str,
        to_id: str,
        dep_type: TaskDependencyType = TaskDependencyType.SEQUENTIAL,
    ) -> None:
        self.edges.append((from_id, to_id, dep_type))
        if from_id in self.nodes:
            if to_id not in self.nodes[from_id].dependents:
                self.nodes[from_id].dependents.append(to_id)
        if to_id in self.nodes:
            if from_id not in self.nodes[to_id].dependencies:
                self.nodes[to_id].dependencies.append(from_id)

    def roots(self) -> list[TaskNode]:
        """Nodes with no dependencies — entry points of the DAG."""
        return [n for n in self.nodes.values() if not n.dependencies]

    def leaves(self) -> list[TaskNode]:
        """Nodes with no dependents — exit points of the DAG."""
        return [n for n in self.nodes.values() if not n.dependents]


# ── Decision & Scoring (9B.3.7) ───────────────────────────────────────────────

@dataclass
class DecisionDimension:
    name: str
    value: float    # 0–1 normalized
    weight: float
    explanation: str = ""

    @property
    def weighted_value(self) -> float:
        return self.value * self.weight


@dataclass
class ExecutionDecision:
    """
    The output of the Decision Engine for a single task placement.

    Carries full traceability: dimensions, scores, reasoning.
    """
    task_id: str
    worker_id: str
    strategy_name: str
    expected_utility: float         # 0–1 composite score
    dimensions: list[DecisionDimension] = field(default_factory=list)
    explanation: str = ""
    alternatives: list[str] = field(default_factory=list)  # other worker_ids considered
    confidence: float = 0.0
    cost_estimate: float = 0.0
    latency_estimate_ms: float = 0.0
    decided_at: str = field(default_factory=_now_iso)


# ── Learning Records (9B.3.6) ─────────────────────────────────────────────────

@dataclass
class ExecutionRecord:
    """
    Immutable record of a completed (or failed) task execution.
    Written by the WorkerRuntime; read by the LearningEngine.
    """
    record_id: str = field(default_factory=_new_id)
    task_id: str = ""
    worker_id: str = ""
    task_type: str = ""
    workflow_id: str = ""

    # AI resource usage
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # Performance
    execution_time_ms: float = 0.0
    latency_ms: float = 0.0
    memory_used_gb: float = 0.0
    gpu_used: bool = False

    # Outcome
    success: bool = True
    failed: bool = False
    retries: int = 0
    error_type: str = ""

    # Cost
    cost: float = 0.0

    # Scheduling context
    strategy_used: str = ""
    capability_score: float = 0.0

    recorded_at: str = field(default_factory=_now_iso)


# ── Knowledge Graph entities (9B.3.8) ─────────────────────────────────────────

class KnowledgeNodeType(str, Enum):
    WORKER     = "worker"
    AGENT      = "agent"
    MODEL      = "model"
    TASK       = "task"
    CAPABILITY = "capability"
    FAILURE    = "failure"
    POLICY     = "policy"
    MEMORY_REF = "memory_ref"
    DEPENDENCY = "dependency"
    TOOL       = "tool"


@dataclass
class KnowledgeNode:
    node_id: str = field(default_factory=_new_id)
    node_type: KnowledgeNodeType = KnowledgeNodeType.TASK
    label: str = ""
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)


@dataclass
class KnowledgeEdge:
    edge_id: str = field(default_factory=_new_id)
    from_node_id: str = ""
    to_node_id: str = ""
    relation: str = ""  # e.g., "executes", "requires", "caused_failure", "uses_model"
    weight: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)


# ── Simulation (9B.3.10) ──────────────────────────────────────────────────────

@dataclass
class SimulationScenario:
    """Input to the RuntimeDigitalTwin simulator."""
    scenario_id: str = field(default_factory=_new_id)
    name: str = ""
    n_workers: int = 10
    n_agents: int = 100
    task_arrival_rate: float = 10.0    # tasks/second
    simulation_duration_seconds: float = 60.0

    # Fault injection
    worker_crash_probability: float = 0.0
    network_partition_probability: float = 0.0
    queue_overflow_threshold: int = 1000
    enable_autoscaling: bool = False
    latency_jitter_ms: float = 0.0

    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulationResult:
    scenario_id: str
    total_tasks_executed: int = 0
    total_tasks_failed: int = 0
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    throughput_rps: float = 0.0
    total_cost: float = 0.0
    worker_utilization: dict[str, float] = field(default_factory=dict)
    policy_violations: int = 0
    autoscale_events: int = 0
    timeline: list[dict[str, Any]] = field(default_factory=list)


# ── Abstract bases for IOC components ─────────────────────────────────────────

class CapabilityMatcher(ABC):
    @abstractmethod
    def score(
        self, profile: CapabilityProfile, requirements: TaskRequirements
    ) -> CapabilityScore:
        """Score how well a worker profile matches task requirements."""

    @abstractmethod
    def rank(
        self,
        profiles: list[CapabilityProfile],
        requirements: TaskRequirements,
    ) -> list[CapabilityScore]:
        """Return profiles ranked by suitability, best first."""


class TaskPlanner(ABC):
    @abstractmethod
    async def plan(self, requirements: list[TaskRequirements]) -> ExecutionGraph:
        """Build an execution graph from a list of task requirements."""


class DecisionEngine(ABC):
    @abstractmethod
    async def decide(
        self,
        requirements: TaskRequirements,
        profiles: list[CapabilityProfile],
    ) -> ExecutionDecision:
        """Select the best worker and return a fully explained decision."""


class LearningEngine(ABC):
    @abstractmethod
    async def record(self, record: ExecutionRecord) -> None:
        """Persist an execution outcome for future learning."""

    @abstractmethod
    async def predict_success_rate(
        self, worker_id: str, task_type: str
    ) -> float:
        """Return the predicted success rate for a worker/task-type pair."""

    @abstractmethod
    async def recommend_model(self, task_type: str) -> str | None:
        """Return the best-performing model for this task type."""
