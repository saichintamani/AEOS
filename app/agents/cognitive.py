"""
AEOS Agent Runtime v2 — 11-Step Cognitive Model

Replaces the v1 think()/act() lifecycle with a structured 11-stage
cognitive pipeline where every step produces a typed artifact.

See docs/architecture/007-AGENT_RUNTIME.md for full specification.

The CognitiveAgent abstract class provides the 11-step scaffolding.
Concrete agents (ResearchAgent, AnalystAgent, etc.) override only
the steps they specialize in; defaults are provided for all steps.

11 Steps:
  1. Observe       — parse and normalize raw inputs
  2. Understand    — classify intent, extract entities, identify constraints
  3. Retrieve      — query memory tiers and RAG for relevant context
  4. Reason        — apply structured reasoning to the understood problem
  5. Hypothesize   — generate 1-N candidate approaches
  6. Evaluate      — score hypotheses against constraints and policy
  7. Plan          — select best hypothesis; build execution plan
  8. Execute       — perform the planned actions (tool calls, LLM calls)
  9. Reflect       — self-critique the execution result
  10. Learn        — extract reusable insights from this execution
  11. Remember     — write insights to memory; update agent state
"""

from __future__ import annotations

import time
import uuid
from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.agents.base import BaseAgent, AgentResponse
from app.core.logger import get_logger

__all__ = [
    "CognitiveStep",
    "ObservationContext",
    "Understanding",
    "RetrievedContext",
    "ReasoningResult",
    "Hypothesis",
    "EvaluatedHypotheses",
    "ExecutionPlan",
    "ExecutionResult",
    "ReflectionResult",
    "LearningResult",
    "MemoryUpdate",
    "CognitiveContext",
    "CognitiveAgent",
    "AbortCode",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AbortCode(str, Enum):
    """Structured abort codes for cognitive cycle failures."""
    MALFORMED_INPUT        = "ABORT_MALFORMED_INPUT"
    CONFLICTING_CONSTRAINTS = "ABORT_CONFLICTING_CONSTRAINTS"
    NO_VIABLE_HYPOTHESIS   = "ABORT_NO_VIABLE_HYPOTHESIS"
    EXECUTION_FAILED       = "ABORT_EXECUTION_FAILED"
    POLICY_VIOLATION       = "ABORT_POLICY_VIOLATION"
    RESOURCE_EXHAUSTED     = "ABORT_RESOURCE_EXHAUSTED"


class CognitiveStep(str, Enum):
    OBSERVE     = "observe"
    UNDERSTAND  = "understand"
    RETRIEVE    = "retrieve"
    REASON      = "reason"
    HYPOTHESIZE = "hypothesize"
    EVALUATE    = "evaluate"
    PLAN        = "plan"
    EXECUTE     = "execute"
    REFLECT     = "reflect"
    LEARN       = "learn"
    REMEMBER    = "remember"


# ── Step Artifacts ─────────────────────────────────────────────────────────────

@dataclass
class ObservationContext:
    """Step 1 artifact: structured view of raw inputs."""
    observation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task: str = ""
    context_keys: list[str] = field(default_factory=list)
    history_length: int = 0
    history_truncated: bool = False
    token_estimate: int = 0
    observed_at: str = field(default_factory=_now)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Understanding:
    """Step 2 artifact: classified intent and extracted constraints."""
    intent_category: str = "UNKNOWN"
    entities: list[dict[str, Any]] = field(default_factory=list)
    hard_constraints: list[str] = field(default_factory=list)
    soft_preferences: list[str] = field(default_factory=list)
    requires_decomposition: bool = False
    ambiguities: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class RetrievedContext:
    """Step 3 artifact: relevant knowledge pulled from memory and RAG."""
    short_term_items: list[dict[str, Any]] = field(default_factory=list)
    long_term_items: list[dict[str, Any]] = field(default_factory=list)
    rag_passages: list[str] = field(default_factory=list)
    retrieval_latency_ms: float = 0.0
    total_items: int = 0


@dataclass
class ReasoningResult:
    """Step 4 artifact: structured reasoning trace."""
    mode: str = "deductive"      # deductive | inductive | abductive | analogical
    premises: list[str] = field(default_factory=list)
    intermediate_conclusions: list[str] = field(default_factory=list)
    final_conclusion: str = ""
    confidence: float = 1.0
    evidence_used: list[str] = field(default_factory=list)


@dataclass
class Hypothesis:
    """A single candidate approach."""
    hypothesis_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    description: str = ""
    approach: str = ""
    expected_outcome: str = ""
    risk_level: str = "low"      # low | medium | high
    feasibility: float = 1.0
    confidence: float = 0.5


@dataclass
class EvaluatedHypotheses:
    """Step 6 artifact: scored and ranked hypotheses."""
    hypotheses: list[Hypothesis] = field(default_factory=list)
    selected: Optional[Hypothesis] = None
    selection_rationale: str = ""
    all_rejected: bool = False
    rejection_reason: str = ""


@dataclass
class ExecutionPlan:
    """Step 7 artifact: the selected approach as an executable plan."""
    plan_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    steps: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    expected_output_format: str = "markdown"
    timeout_budget_ms: float = 30_000.0


@dataclass
class ExecutionResult:
    """Step 8 artifact: outcome of executing the plan."""
    success: bool = False
    output: Any = None
    tool_outputs: list[dict[str, Any]] = field(default_factory=list)
    token_cost: int = 0
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class ReflectionResult:
    """Step 9 artifact: self-critique of the execution."""
    quality_score: float = 1.0
    completeness: float = 1.0
    accuracy: float = 1.0
    issues_found: list[str] = field(default_factory=list)
    passes_criteria: bool = True
    needs_revision: bool = False
    revision_suggestions: list[str] = field(default_factory=list)


@dataclass
class LearningResult:
    """Step 10 artifact: insights extracted from this execution."""
    insights: list[str] = field(default_factory=list)
    pattern_identified: str = ""
    applicable_future_tasks: list[str] = field(default_factory=list)
    confidence: float = 0.5


@dataclass
class MemoryUpdate:
    """Step 11 artifact: what gets written to memory."""
    short_term_writes: dict[str, Any] = field(default_factory=dict)
    long_term_writes: dict[str, Any] = field(default_factory=dict)
    agent_state_updates: dict[str, Any] = field(default_factory=dict)
    written_at: str = field(default_factory=_now)


@dataclass
class CognitiveContext:
    """
    Carries all step artifacts through the 11-step pipeline.
    Accumulates data as each step completes.
    """
    agent_id: str = ""
    task: str = ""
    raw_context: dict[str, Any] = field(default_factory=dict)
    trace_id: str = ""
    cycle_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: str = field(default_factory=_now)

    # Step artifacts (populated as pipeline progresses)
    observation: Optional[ObservationContext] = None
    understanding: Optional[Understanding] = None
    retrieved: Optional[RetrievedContext] = None
    reasoning: Optional[ReasoningResult] = None
    hypotheses: Optional[EvaluatedHypotheses] = None
    plan: Optional[ExecutionPlan] = None
    execution: Optional[ExecutionResult] = None
    reflection: Optional[ReflectionResult] = None
    learning: Optional[LearningResult] = None
    memory_update: Optional[MemoryUpdate] = None

    # Abort state
    aborted: bool = False
    abort_code: Optional[AbortCode] = None
    abort_reason: str = ""

    # Step timing
    step_latencies: dict[str, float] = field(default_factory=dict)


# ── CognitiveAgent ─────────────────────────────────────────────────────────────

class CognitiveAgent(BaseAgent):
    """
    Abstract base for AEOS v2 agents using the 11-step cognitive model.

    Subclasses override only the steps they specialize.
    All 11 steps have sensible defaults that produce minimal but valid artifacts.

    The run() method from BaseAgent is implemented here to orchestrate
    all 11 steps. Subclasses do NOT override run() — they override individual
    step methods.
    """

    async def run(self, task: str, context: dict) -> AgentResponse:
        """Execute the full 11-step cognitive pipeline."""
        ctx = CognitiveContext(
            agent_id=self.id,
            task=task,
            raw_context=context,
            trace_id=context.get("trace_id", ""),
        )
        t_total = time.perf_counter()

        steps = [
            (CognitiveStep.OBSERVE,     self._step_observe),
            (CognitiveStep.UNDERSTAND,  self._step_understand),
            (CognitiveStep.RETRIEVE,    self._step_retrieve),
            (CognitiveStep.REASON,      self._step_reason),
            (CognitiveStep.HYPOTHESIZE, self._step_hypothesize),
            (CognitiveStep.EVALUATE,    self._step_evaluate),
            (CognitiveStep.PLAN,        self._step_plan),
            (CognitiveStep.EXECUTE,     self._step_execute),
            (CognitiveStep.REFLECT,     self._step_reflect),
            (CognitiveStep.LEARN,       self._step_learn),
            (CognitiveStep.REMEMBER,    self._step_remember),
        ]

        for step_name, step_fn in steps:
            if ctx.aborted:
                break
            t_step = time.perf_counter()
            try:
                await step_fn(ctx)
            except Exception as exc:
                log.error(
                    "Cognitive step failed",
                    extra={"ctx_agent": self.id, "ctx_step": step_name.value, "ctx_error": str(exc)},
                )
                ctx.aborted = True
                ctx.abort_code = AbortCode.EXECUTION_FAILED
                ctx.abort_reason = str(exc)
            finally:
                ctx.step_latencies[step_name.value] = round((time.perf_counter() - t_step) * 1000, 1)

        total_latency = round((time.perf_counter() - t_total) * 1000, 1)

        if ctx.aborted:
            return AgentResponse(
                agent_id=self.id,
                agent_name=self.name,
                status="failed",
                result=None,
                error=f"{ctx.abort_code.value if ctx.abort_code else 'UNKNOWN'}: {ctx.abort_reason}",
                thought=f"Aborted at cognitive cycle. Latency: {total_latency}ms",
                latency_ms=total_latency,
            )

        result = self._build_result(ctx)
        return AgentResponse(
            agent_id=self.id,
            agent_name=self.name,
            status="success",
            result=result,
            error="",
            thought=self._build_thought(ctx),
            latency_ms=total_latency,
        )

    # ── Step 1: Observe ────────────────────────────────────────────────────────

    async def _step_observe(self, ctx: CognitiveContext) -> None:
        if not ctx.task or not ctx.task.strip():
            ctx.aborted = True
            ctx.abort_code = AbortCode.MALFORMED_INPUT
            ctx.abort_reason = "Task string is null or empty"
            return

        ctx.observation = ObservationContext(
            task=ctx.task.strip(),
            context_keys=list(ctx.raw_context.keys()),
            history_length=len(ctx.raw_context.get("history", [])),
            token_estimate=len(ctx.task.split()) * 4 // 3,
        )

    # ── Step 2: Understand ─────────────────────────────────────────────────────

    async def _step_understand(self, ctx: CognitiveContext) -> None:
        if ctx.observation is None:
            return
        ctx.understanding = Understanding(
            intent_category=self._classify_intent(ctx.observation.task),
            hard_constraints=self._extract_constraints(ctx.observation.task),
            confidence=0.8,
        )

    def _classify_intent(self, task: str) -> str:
        task_l = task.lower()
        if any(w in task_l for w in ("analyze", "analyse", "evaluate", "assess")):
            return "ANALYZE"
        if any(w in task_l for w in ("search", "find", "research", "retrieve")):
            return "QUERY"
        if any(w in task_l for w in ("generate", "write", "create", "build")):
            return "GENERATE"
        if any(w in task_l for w in ("summarize", "summary", "synthesize")):
            return "SUMMARIZE"
        if any(w in task_l for w in ("plan", "decompose", "organize")):
            return "PLAN"
        return "QUERY"

    def _extract_constraints(self, task: str) -> list[str]:
        constraints: list[str] = []
        if "json" in task.lower():
            constraints.append("output_format=json")
        if "under" in task.lower() and any(c.isdigit() for c in task):
            constraints.append("length_constraint")
        return constraints

    # ── Step 3: Retrieve ───────────────────────────────────────────────────────

    async def _step_retrieve(self, ctx: CognitiveContext) -> None:
        t0 = time.perf_counter()
        # Default: read from context (no external memory call)
        upstream = ctx.raw_context.get("upstream_results", {})
        short_term = [{"key": k, "value": str(v)[:200]} for k, v in upstream.items()]
        ctx.retrieved = RetrievedContext(
            short_term_items=short_term,
            retrieval_latency_ms=round((time.perf_counter() - t0) * 1000, 1),
            total_items=len(short_term),
        )

    # ── Step 4: Reason ─────────────────────────────────────────────────────────

    async def _step_reason(self, ctx: CognitiveContext) -> None:
        if ctx.understanding is None:
            return
        premises = [f"Task: {ctx.task[:100]}"]
        if ctx.retrieved and ctx.retrieved.short_term_items:
            premises.append(f"Available context: {len(ctx.retrieved.short_term_items)} items")
        ctx.reasoning = ReasoningResult(
            mode="deductive",
            premises=premises,
            final_conclusion=f"Apply {ctx.understanding.intent_category} reasoning to: {ctx.task[:80]}",
            confidence=ctx.understanding.confidence,
        )

    # ── Step 5: Hypothesize ────────────────────────────────────────────────────

    async def _step_hypothesize(self, ctx: CognitiveContext) -> None:
        ctx.hypotheses = EvaluatedHypotheses(
            hypotheses=[
                Hypothesis(
                    description="Direct approach",
                    approach=f"Process task directly using agent capabilities",
                    expected_outcome="Satisfactory result within constraints",
                    feasibility=0.9,
                    confidence=0.8,
                )
            ]
        )

    # ── Step 6: Evaluate ───────────────────────────────────────────────────────

    async def _step_evaluate(self, ctx: CognitiveContext) -> None:
        if ctx.hypotheses is None or not ctx.hypotheses.hypotheses:
            ctx.aborted = True
            ctx.abort_code = AbortCode.NO_VIABLE_HYPOTHESIS
            ctx.abort_reason = "No hypotheses to evaluate"
            return

        # Select the highest-confidence hypothesis
        best = max(ctx.hypotheses.hypotheses, key=lambda h: h.confidence * h.feasibility)
        ctx.hypotheses.selected = best
        ctx.hypotheses.selection_rationale = f"Selected '{best.description}' (confidence={best.confidence})"

    # ── Step 7: Plan ───────────────────────────────────────────────────────────

    async def _step_plan(self, ctx: CognitiveContext) -> None:
        if ctx.hypotheses is None or ctx.hypotheses.selected is None:
            return
        ctx.plan = ExecutionPlan(
            steps=["Process task", "Format result"],
            expected_output_format="markdown",
        )

    # ── Step 8: Execute ────────────────────────────────────────────────────────

    @abstractmethod
    async def _step_execute(self, ctx: CognitiveContext) -> None:
        """
        Execute the selected plan. Must populate ctx.execution.

        This is the only step that is abstract — all other steps have defaults.
        Subclasses implement their domain-specific logic here (LLM calls, tool
        invocations, RAG queries, etc.).
        """

    # ── Step 9: Reflect ────────────────────────────────────────────────────────

    async def _step_reflect(self, ctx: CognitiveContext) -> None:
        if ctx.execution is None:
            return

        has_output = ctx.execution.output is not None and ctx.execution.output != ""
        quality = 0.85 if has_output and ctx.execution.success else 0.3

        ctx.reflection = ReflectionResult(
            quality_score=quality,
            completeness=quality,
            passes_criteria=quality >= 0.7,
            needs_revision=quality < 0.7,
        )

    # ── Step 10: Learn ─────────────────────────────────────────────────────────

    async def _step_learn(self, ctx: CognitiveContext) -> None:
        insights: list[str] = []
        if ctx.reflection and ctx.reflection.issues_found:
            insights.append(f"Issues encountered: {ctx.reflection.issues_found}")
        if ctx.execution and ctx.execution.success:
            insights.append(f"Successful approach: {ctx.hypotheses.selected.approach if ctx.hypotheses and ctx.hypotheses.selected else 'unknown'}")

        ctx.learning = LearningResult(
            insights=insights,
            confidence=0.6,
        )

    # ── Step 11: Remember ──────────────────────────────────────────────────────

    async def _step_remember(self, ctx: CognitiveContext) -> None:
        if not ctx.execution or not ctx.execution.success:
            ctx.memory_update = MemoryUpdate()
            return

        ctx.memory_update = MemoryUpdate(
            long_term_writes={
                ctx.task[:50]: {
                    "result_preview": str(ctx.execution.output)[:200] if ctx.execution.output else "",
                    "agent_id": self.id,
                    "quality": ctx.reflection.quality_score if ctx.reflection else 0.0,
                }
            }
        )

        # Write to long-term memory
        try:
            from app.core.memory import get_memory
            memory = get_memory()
            for key, value in ctx.memory_update.long_term_writes.items():
                memory.write_long(key=key, value=value, agent_id=self.id, task_id=ctx.cycle_id)
        except Exception:
            pass  # Memory failure should not abort the agent

    # ── Result builder ─────────────────────────────────────────────────────────

    def _build_result(self, ctx: CognitiveContext) -> Any:
        """Extract the final result from the cognitive context."""
        if ctx.execution and ctx.execution.output is not None:
            return ctx.execution.output
        return None

    def _build_thought(self, ctx: CognitiveContext) -> str:
        """Build a human-readable thought trace from the cognitive context."""
        parts = []
        if ctx.understanding:
            parts.append(f"Intent: {ctx.understanding.intent_category}")
        if ctx.reasoning:
            parts.append(f"Reasoning: {ctx.reasoning.final_conclusion[:80]}")
        if ctx.reflection:
            parts.append(f"Quality: {ctx.reflection.quality_score:.2f}")
        return " | ".join(parts) if parts else "Cognitive cycle complete."

    # ── BaseAgent abstract methods ─────────────────────────────────────────────

    async def think(self, task: str) -> str:
        """v1 compatibility shim — delegates to cognitive pipeline."""
        return f"CognitiveAgent.think for: {task[:80]}"

    async def act(self, thought: str, context: dict) -> Any:
        """v1 compatibility shim — not used in v2."""
        return None
