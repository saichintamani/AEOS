"""
Wave 9B.3.9 — Explainability Engine

Produces human-readable explanations at multiple verbosity levels for:
  - ExecutionDecisions (worker selection)
  - ReasoningTraces (CoT)
  - CapabilityScores (dimensional breakdown)

ExplanationEngine is the single entry point.
"""

from __future__ import annotations

import logging
from enum import Enum

from app.runtime_intelligence.contracts import (
    CapabilityScore,
    ExecutionDecision,
)
from app.runtime_intelligence.reasoning import ReasoningTrace

logger = logging.getLogger(__name__)


class Verbosity(str, Enum):
    BRIEF   = "brief"    # one sentence
    NORMAL  = "normal"   # key facts
    FULL    = "full"     # all dimensions + CoT


class ExplanationEngine:
    """
    Renders decisions and traces as structured natural language.
    """

    def explain_decision(
        self,
        decision: ExecutionDecision,
        verbosity: Verbosity = Verbosity.NORMAL,
    ) -> str:
        if not decision.worker_id:
            return "No worker could be selected — no eligible candidates."

        if verbosity == Verbosity.BRIEF:
            return (
                f"Selected {decision.worker_id} for task {decision.task_id} "
                f"(utility={decision.expected_utility:.3f})."
            )

        if verbosity == Verbosity.NORMAL:
            parts = [
                f"Worker {decision.worker_id} selected for task {decision.task_id}.",
                f"Strategy: {decision.strategy_name}.",
                f"Expected utility: {decision.expected_utility:.3f}, confidence: {decision.confidence:.3f}.",
            ]
            if decision.alternatives:
                parts.append(f"Alternatives considered: {', '.join(decision.alternatives[:3])}.")
            if decision.cost_estimate:
                parts.append(f"Estimated cost: ${decision.cost_estimate:.4f}.")
            if decision.latency_estimate_ms:
                parts.append(f"Estimated latency: {decision.latency_estimate_ms:.0f}ms.")
            return " ".join(parts)

        # FULL
        lines = [
            f"# Decision: task={decision.task_id}",
            f"Worker selected: {decision.worker_id}",
            f"Strategy: {decision.strategy_name}",
            f"Expected utility: {decision.expected_utility:.4f}",
            f"Confidence: {decision.confidence:.4f}",
            "",
            "## Score Dimensions",
        ]
        for dim in decision.dimensions:
            bar = "█" * int(dim.value * 20)
            lines.append(
                f"  {dim.name:<12} {dim.value:.3f} (w={dim.weight:.2f})  {bar}"
            )
        if decision.alternatives:
            lines.append(f"\nAlternatives: {', '.join(decision.alternatives)}")
        if decision.explanation:
            lines.append(f"\nExplanation: {decision.explanation}")
        return "\n".join(lines)

    def explain_score(
        self,
        score: CapabilityScore,
        verbosity: Verbosity = Verbosity.NORMAL,
    ) -> str:
        if verbosity == Verbosity.BRIEF:
            return f"Worker {score.worker_id}: score={score.total_score:.3f}"

        lines = [
            f"Worker {score.worker_id} | total={score.total_score:.3f}",
            f"  capability={score.capability_score:.3f}  trust={score.trust_score:.3f}"
            f"  load={score.load_score:.3f}  latency={score.latency_score:.3f}"
            f"  cost={score.cost_score:.3f}  locality={score.locality_score:.3f}",
        ]
        if verbosity == Verbosity.FULL and score.explanation:
            lines.append(f"  {score.explanation}")
        return "\n".join(lines)

    def explain_trace(self, trace: ReasoningTrace) -> str:
        return trace.cot_text or f"No reasoning trace available for task {trace.task_id}."

    def decision_to_dict(self, decision: ExecutionDecision) -> dict:
        return {
            "task_id": decision.task_id,
            "worker_id": decision.worker_id,
            "strategy": decision.strategy_name,
            "utility": decision.expected_utility,
            "confidence": decision.confidence,
            "dimensions": {
                d.name: {"value": d.value, "weight": d.weight}
                for d in decision.dimensions
            },
            "alternatives": decision.alternatives,
            "cost_estimate": decision.cost_estimate,
            "latency_estimate_ms": decision.latency_estimate_ms,
            "explanation": decision.explanation,
            "decided_at": decision.decided_at,
        }
