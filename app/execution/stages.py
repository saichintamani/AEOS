"""
AEOS Execution Engine — 15-Stage Pipeline

Each stage is a pure function (or lightweight class) that takes typed input
and produces typed output. Side effects (memory writes, event emissions) are
explicit and logged.

See docs/architecture/006-EXECUTION_ENGINE.md for full specifications.
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from app.core.logger import get_logger
from app.execution.schemas import (
    AggregatedResult,
    AuditRecord,
    ClassifiedIntent,
    ConstraintDegradation,
    ConstraintSet,
    ConstraintSolution,
    CostBreakdown,
    Entity,
    ExecutionError,
    ExecutionGraph,
    ExecutionPlan,
    Goal,
    GoalEvaluation,
    GoalSet,
    GoalType,
    GovernanceDecision,
    GovernanceGateResult,
    RawIntent,
    ReflectionDecision,
    ReflectionGateResult,
    StepResult,
    StepStatus,
    SuccessCriteria,
    TaskType,
    ValidatedInput,
    WorkflowState,
    WorkflowStatus,
)

__all__ = [
    "stage1_receive_intent",
    "stage2_validate_input",
    "stage3_classify_intent",
    "stage4_collect_constraints",
    "stage5_solve_constraints",
    "stage6_decompose_goals",
    "stage7_build_goals",
    "stage8_plan",
    "stage9_compile_graph",
    "stage10_workflow_entry",
    "stage13_aggregate",
    "stage14_reflection_gate",
    "stage15_governance_gate",
]

log = get_logger(__name__)

_MAX_TASK_LENGTH = 8_192

# Keyword sets for task type classification
_RESEARCH_KW  = {"research", "find", "search", "look", "gather", "discover", "retrieve", "fetch", "get"}
_ANALYST_KW   = {"analyze", "analyse", "evaluate", "assess", "compare", "examine", "inspect", "review"}
_EXECUTOR_KW  = {"execute", "run", "deploy", "perform", "implement", "apply", "process", "do", "create", "build", "write", "generate"}
_PLANNER_KW   = {"plan", "decompose", "structure", "organize", "schedule", "breakdown", "design"}
_SYNTH_KW     = {"summarize", "synthesize", "combine", "merge", "consolidate", "aggregate"}


# ── Stage 1: Intent Reception ──────────────────────────────────────────────────

def stage1_receive_intent(
    raw_task: str,
    mode: str = "auto",
    caller_id: str = "anonymous",
    request_id: str = "",
) -> RawIntent | ExecutionError:
    if not raw_task or not raw_task.strip():
        return ExecutionError(code="EMPTY_TASK", message="Task string is empty.", stage=1)

    if mode not in ("auto", "plan_only", "dry_run", "sync", "multi-agent", "single-agent"):
        return ExecutionError(code="INVALID_MODE", message=f"Unknown mode: {mode!r}", stage=1)

    return RawIntent(
        raw_task=raw_task,
        mode=mode,
        caller_id=caller_id,
        request_id=request_id or str(uuid.uuid4()),
    )


# ── Stage 2: Input Validation ──────────────────────────────────────────────────

def stage2_validate_input(raw: RawIntent) -> ValidatedInput | ExecutionError:
    task = raw.raw_task.strip()
    task = re.sub(r"[ \t]+", " ", task)  # normalize internal whitespace

    if len(task) > _MAX_TASK_LENGTH:
        return ExecutionError(
            code="TASK_TOO_LONG",
            message=f"Task length {len(task)} exceeds maximum {_MAX_TASK_LENGTH}.",
            stage=2,
            trace_id=raw.trace_id,
        )

    # Check for null bytes / non-printable control chars (except newlines, tabs)
    if any(0 < ord(c) < 9 or 11 <= ord(c) < 32 for c in task):
        return ExecutionError(code="INVALID_ENCODING", message="Task contains invalid control characters.", stage=2, trace_id=raw.trace_id)

    warnings: list[str] = []

    # Injection pattern detection
    injection_patterns = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"system\s*prompt",
        r"<\s*/?system\s*>",
    ]
    for pattern in injection_patterns:
        if re.search(pattern, task, re.IGNORECASE):
            warnings.append("possible_prompt_injection_detected")
            break

    return ValidatedInput(
        task=task,
        mode=raw.mode,
        caller_id=raw.caller_id,
        trace_id=raw.trace_id,
        policy_warnings=warnings,
    )


# ── Stage 3: Intent Understanding ─────────────────────────────────────────────

def stage3_classify_intent(validated: ValidatedInput) -> ClassifiedIntent:
    task_lower = validated.task.lower()
    words = set(task_lower.split())

    intents: dict[str, TaskType] = {}
    if words & _RESEARCH_KW:   intents["research"] = TaskType.RESEARCH
    if words & _ANALYST_KW:    intents["analysis"] = TaskType.ANALYSIS
    if words & _EXECUTOR_KW:   intents["execution"] = TaskType.EXECUTION
    if words & _PLANNER_KW:    intents["planning"] = TaskType.PLANNING
    if words & _SYNTH_KW:      intents["synthesis"] = TaskType.SYNTHESIS

    if len(intents) >= 2 or len(validated.task) > 200:
        task_type = TaskType.MULTI_STEP
        estimated_steps = min(len(intents) + 1, 6)
        complexity = "complex" if len(intents) >= 3 else "medium"
    elif len(intents) == 1:
        task_type = next(iter(intents.values()))
        estimated_steps = 2
        complexity = "medium" if len(validated.task) > 100 else "simple"
    else:
        task_type = TaskType.CONVERSATIONAL
        estimated_steps = 1
        complexity = "simple"

    # Naive entity extraction
    entities = _extract_entities(validated.task)

    return ClassifiedIntent(
        task=validated.task,
        task_type=task_type,
        mode=validated.mode,
        entities=entities,
        estimated_steps=estimated_steps,
        complexity=complexity,
        trace_id=validated.trace_id,
        policy_warnings=validated.policy_warnings,
    )


def _extract_entities(task: str) -> list[Entity]:
    entities: list[Entity] = []
    # URLs
    for m in re.finditer(r"https?://\S+", task):
        entities.append(Entity(entity_type="url", value=m.group(), span=(m.start(), m.end())))
    # File paths
    for m in re.finditer(r"(?:^|[\s\"\'])([./\\][\w./\\-]+\.\w{1,6})", task):
        entities.append(Entity(entity_type="file_path", value=m.group(1), span=(m.start(1), m.end(1))))
    return entities[:10]  # cap at 10


# ── Stage 4: Constraint Collection ────────────────────────────────────────────

def stage4_collect_constraints(
    intent: ClassifiedIntent,
    settings: Any = None,
) -> ConstraintSet:
    cs = ConstraintSet()

    # Mode-specific adjustments
    if intent.task_type == TaskType.MULTI_STEP:
        cs.max_parallel_agents = 3
        cs.require_reflection = True
    elif intent.task_type == TaskType.CONVERSATIONAL:
        cs.max_parallel_agents = 1
        cs.require_reflection = False

    if intent.complexity == "complex":
        cs.timeout_seconds = 180.0
        cs.max_steps = 15
    elif intent.complexity == "simple":
        cs.timeout_seconds = 60.0
        cs.max_steps = 5

    return cs


# ── Stage 5: Constraint Solving ────────────────────────────────────────────────

def stage5_solve_constraints(
    intent: ClassifiedIntent,
    constraints: ConstraintSet,
    available_agent_ids: list[str],
) -> ConstraintSolution | ExecutionError:
    degradations: list[ConstraintDegradation] = []
    unsatisfied: list[str] = []

    if not available_agent_ids:
        return ExecutionError(
            code="NO_AGENTS_AVAILABLE",
            message="No agents are registered and available.",
            stage=5,
            trace_id=intent.trace_id,
        )

    # Sync mode → sequential only
    effective_max_parallel = constraints.max_parallel_agents
    if intent.mode == "sync" or "sync" in constraints.mode_overrides.get("mode", ""):
        effective_max_parallel = 1
        degradations.append(ConstraintDegradation(
            constraint="max_parallel_agents",
            original_value=constraints.max_parallel_agents,
            degraded_value=1,
            reason="sync mode forces sequential execution",
        ))

    return ConstraintSolution(
        satisfiable=True,
        available_agents=available_agent_ids,
        available_tools=[],
        effective_timeout_seconds=constraints.timeout_seconds,
        effective_max_parallel=effective_max_parallel,
        degradations=degradations,
        unsatisfied_hard_constraints=unsatisfied,
    )


# ── Stage 6: Goal Decomposition ───────────────────────────────────────────────

def stage6_decompose_goals(
    intent: ClassifiedIntent,
    solution: ConstraintSolution,
) -> GoalSet | ExecutionError:
    goals: list[Goal] = []

    if intent.task_type in (TaskType.CONVERSATIONAL, TaskType.RESEARCH):
        goals.append(Goal(
            description=intent.task,
            goal_type=GoalType.INFORMATION,
            priority=1,
        ))
    elif intent.task_type in (TaskType.ANALYSIS,):
        goals.append(Goal(
            description=intent.task,
            goal_type=GoalType.INFORMATION,
            priority=1,
        ))
        goals.append(Goal(
            description=f"Analyze findings from: {intent.task[:80]}",
            goal_type=GoalType.EVALUATION,
            priority=2,
        ))
    elif intent.task_type in (TaskType.EXECUTION,):
        goals.append(Goal(
            description=f"Plan execution for: {intent.task[:80]}",
            goal_type=GoalType.ACTION,
            priority=1,
        ))
        goals.append(Goal(
            description=intent.task,
            goal_type=GoalType.ACTION,
            priority=2,
            dependency_ids=[goals[0].goal_id],
        ))
    elif intent.task_type in (TaskType.MULTI_STEP, TaskType.PLANNING):
        # Decompose into research → analyze → synthesize
        g1 = Goal(description=f"Research: {intent.task[:80]}", goal_type=GoalType.INFORMATION, priority=1)
        g2 = Goal(description=f"Analyze research results for: {intent.task[:80]}", goal_type=GoalType.EVALUATION, priority=2, dependency_ids=[g1.goal_id])
        g3 = Goal(description=f"Synthesize final answer for: {intent.task[:80]}", goal_type=GoalType.SYNTHESIS, priority=3, dependency_ids=[g2.goal_id])
        goals = [g1, g2, g3]
    elif intent.task_type == TaskType.SYNTHESIS:
        goals.append(Goal(description=intent.task, goal_type=GoalType.SYNTHESIS, priority=1))
    else:
        goals.append(Goal(description=intent.task, goal_type=GoalType.INFORMATION, priority=1))

    if not goals:
        return ExecutionError(code="DECOMPOSITION_FAILED", message="Goal decomposition produced no goals.", stage=6, trace_id=intent.trace_id)

    strategy = "sequential" if len(goals) > 1 else "simple"
    return GoalSet(goals=goals, decomposition_strategy=strategy)


# ── Stage 7: Goal Building ─────────────────────────────────────────────────────

def stage7_build_goals(
    goal_set: GoalSet,
    constraints: ConstraintSet,
) -> GoalSet:
    total_ms = constraints.timeout_seconds * 1000
    n = len(goal_set.goals)

    for i, goal in enumerate(goal_set.goals):
        # Distribute deadline proportionally (earlier goals get more time)
        goal.deadline_ms = total_ms / n
        goal.resource_budget_tokens = constraints.max_cost_tokens // max(n, 1)
        goal.success_criteria = SuccessCriteria(
            evaluation_metrics=["COMPLETENESS", "RELEVANCE"],
        )
        if not goal.fallback_description:
            goal.fallback_description = f"Provide best available answer for: {goal.description[:60]}"

    return goal_set


# ── Stage 8: Planning ──────────────────────────────────────────────────────────

_GOAL_TYPE_TO_AGENT: dict[GoalType, str] = {
    GoalType.INFORMATION: "research_agent",
    GoalType.EVALUATION:  "analyst_agent",
    GoalType.ACTION:      "executor_agent",
    GoalType.SYNTHESIS:   "simple_agent",
}

_FALLBACK_CHAIN: dict[str, list[str]] = {
    "research_agent":  ["simple_agent"],
    "analyst_agent":   ["simple_agent"],
    "executor_agent":  ["simple_agent"],
    "planner_agent":   ["simple_agent"],
}


def stage8_plan(
    goal_set: GoalSet,
    solution: ConstraintSolution,
    trace_id: str = "",
) -> ExecutionPlan:
    from app.execution.schemas import AgentAssignment

    assignments = []
    for goal in goal_set.goals:
        preferred = _GOAL_TYPE_TO_AGENT.get(goal.goal_type, "simple_agent")
        # Select best available agent
        primary = preferred if preferred in solution.available_agents else (
            solution.available_agents[0] if solution.available_agents else "simple_agent"
        )
        fallbacks = [f for f in _FALLBACK_CHAIN.get(primary, []) if f in solution.available_agents and f != primary]
        assignments.append(AgentAssignment(
            goal_id=goal.goal_id,
            primary_agent_type=primary,
            fallback_agent_types=fallbacks,
        ))

    return ExecutionPlan(
        assignments=assignments,
        goal_set=goal_set,
        execution_strategy="topological",
        trace_id=trace_id,
    )


# ── Stage 9: Graph Compilation ─────────────────────────────────────────────────

def stage9_compile_graph(
    plan: ExecutionPlan,
    agent_registry: dict[str, Any],
    total_timeout_ms: float = 120_000.0,
) -> ExecutionGraph | ExecutionError:
    from app.execution.graph import AgentNode, SequentialEdge, GraphCompiler

    nodes: list[AgentNode] = []
    edges: list[SequentialEdge] = []
    node_map: dict[str, str] = {}  # goal_id → node_id

    goal_map = {g.goal_id: g for g in plan.goal_set.goals}

    for assignment in plan.assignments:
        goal = goal_map[assignment.goal_id]
        node = AgentNode(
            goal_id=assignment.goal_id,
            agent_type=assignment.primary_agent_type,
            task_description=goal.description,
            timeout_ms=goal.deadline_ms,
            priority=goal.priority,
        )
        nodes.append(node)
        node_map[assignment.goal_id] = node.node_id

    # Build edges from goal dependencies
    for goal in plan.goal_set.goals:
        for dep_goal_id in goal.dependency_ids:
            from_node_id = node_map.get(dep_goal_id)
            to_node_id = node_map.get(goal.goal_id)
            if from_node_id and to_node_id:
                edges.append(SequentialEdge(from_node_id=from_node_id, to_node_id=to_node_id))

    try:
        compiler = GraphCompiler()
        graph = compiler.compile(
            nodes=nodes,  # type: ignore[arg-type]
            edges=edges,  # type: ignore[arg-type]
            total_timeout_ms=total_timeout_ms,
            trace_id=plan.trace_id,
            plan_id=plan.plan_id,
        )
    except ValueError as exc:
        return ExecutionError(code="GRAPH_COMPILE_ERROR", message=str(exc), stage=9, trace_id=plan.trace_id)

    return graph


# ── Stage 10: Workflow Entry ───────────────────────────────────────────────────

def stage10_workflow_entry(
    graph: ExecutionGraph,
    task_id: str,
    trace_id: str = "",
) -> WorkflowState:
    state = WorkflowState(
        execution_graph=graph,
        status=WorkflowStatus.PLANNING,
        task_id=task_id,
        trace_id=trace_id,
    )
    state.status = WorkflowStatus.EXECUTING
    return state


# ── Stage 13: Aggregation ──────────────────────────────────────────────────────

def stage13_aggregate(
    workflow_state: WorkflowState,
    goal_set: GoalSet,
) -> AggregatedResult:
    completed = [
        sr for sr in workflow_state.step_results.values()
        if sr.status == StepStatus.COMPLETED and sr.value
    ]
    failed_count = len(workflow_state.failed_nodes)
    total = len(workflow_state.step_results)

    partial_ratio = len(completed) / max(total, 1)

    # Merge content
    parts: list[str] = []
    agents_used: list[str] = []
    total_tokens = 0
    total_latency = 0.0
    total_confidence = 0.0

    for sr in sorted(completed, key=lambda r: r.produced_at):
        if isinstance(sr.value, str):
            parts.append(sr.value)
        elif isinstance(sr.value, dict):
            for k in ("result", "summary", "content", "answer", "synthesis", "conclusions"):
                if k in sr.value:
                    parts.append(str(sr.value[k]))
                    break
            else:
                parts.append(str(sr.value))
        if sr.agent_id and sr.agent_id not in agents_used:
            agents_used.append(sr.agent_id)
        total_tokens += sr.token_cost
        total_latency += sr.latency_ms
        total_confidence += sr.confidence

    content = "\n\n".join(parts) if parts else "No results were produced."
    avg_confidence = total_confidence / max(len(completed), 1)

    return AggregatedResult(
        content=content,
        partial=failed_count > 0,
        partial_success_ratio=round(partial_ratio, 3),
        total_token_cost=total_tokens,
        total_latency_ms=round(total_latency, 1),
        agents_used=agents_used,
        confidence=round(avg_confidence, 3),
    )


# ── Stage 14: Reflection Gate ──────────────────────────────────────────────────

def stage14_reflection_gate(
    aggregated: AggregatedResult,
    goal_set: GoalSet,
    revision_count: int = 0,
    max_revisions: int = 2,
    quality_threshold: float = 0.75,
) -> ReflectionGateResult:
    goal_evals: list[GoalEvaluation] = []
    warnings: list[str] = []

    for goal in goal_set.goals:
        # Heuristic quality scoring
        completeness = min(1.0, len(aggregated.content) / max(goal.success_criteria.min_word_count * 5, 100))
        relevance = aggregated.confidence
        coherence = 1.0 if not aggregated.partial else 0.7
        score = round((completeness * 0.4 + relevance * 0.4 + coherence * 0.2), 3)
        goal_evals.append(GoalEvaluation(
            goal_id=goal.goal_id,
            quality_score=score,
            completeness=completeness,
            relevance=relevance,
            coherence=coherence,
        ))

    overall = round(sum(e.quality_score for e in goal_evals) / max(len(goal_evals), 1), 3)

    if overall >= quality_threshold:
        decision = ReflectionDecision.PASS
    elif revision_count < max_revisions:
        decision = ReflectionDecision.REVISE
        warnings.append(f"quality_below_threshold: {overall:.2f} < {quality_threshold}")
    else:
        decision = ReflectionDecision.PASS_WARNED
        warnings.append(f"max_revisions_reached; quality_score={overall}")

    return ReflectionGateResult(
        decision=decision,
        overall_quality_score=overall,
        goal_evaluations=goal_evals,
        warnings=warnings,
    )


# ── Stage 15: Governance Gate ──────────────────────────────────────────────────

def stage15_governance_gate(
    aggregated: AggregatedResult,
    reflection: ReflectionGateResult,
    workflow_state: WorkflowState,
    task_id: str = "",
    caller_id: str = "anonymous",
    trace_id: str = "",
    t_start: float = 0.0,
) -> GovernanceGateResult:
    # Policy compliance (simplified: no PII detection in v2.0 — v3 adds ML-based PII detector)
    decision = GovernanceDecision.PASS
    content = aggregated.content

    warnings = list(reflection.warnings)
    if aggregated.partial:
        warnings.append(f"partial_result: {aggregated.partial_success_ratio:.0%} nodes succeeded")

    status = "completed"
    if aggregated.partial:
        status = "completed_partial"
    if not aggregated.content or aggregated.content == "No results were produced.":
        status = "failed"

    latency_ms = round((time.time() - t_start) * 1000, 1) if t_start else aggregated.total_latency_ms

    cost = CostBreakdown(
        token_cost_tokens=aggregated.total_token_cost,
        token_cost_usd=round(aggregated.total_token_cost / 1_000_000 * 3.0, 6),  # ~$3/1M tokens
    )
    cost.total_usd = cost.token_cost_usd

    # Thought: execution summary
    thought = (
        f"Executed {len(workflow_state.completed_nodes)} node(s), "
        f"{len(workflow_state.failed_nodes)} failed, "
        f"quality={reflection.overall_quality_score:.2f}. "
        f"Agents: {', '.join(aggregated.agents_used) or 'none'}."
    )

    return GovernanceGateResult(
        governance_decision=decision,
        status=status,
        result=content,
        thought=thought,
        trace_id=trace_id,
        quality_score=reflection.overall_quality_score,
        latency_ms=latency_ms,
        cost=cost,
        warnings=warnings,
        agents_used=aggregated.agents_used,
    )
