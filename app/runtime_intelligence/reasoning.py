"""
Wave 9B.3.3 — Agent Reasoning Layer

Provides structured reasoning traces for scheduling decisions.

ReasoningContext  — accumulates observations, hypotheses, and conclusions
ReasoningEngine   — produces a ReasoningTrace for a (task, candidates) pair
ChainOfThought    — formats a trace as human-readable CoT text
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    CapabilityScore,
    TaskRequirements,
)

logger = logging.getLogger(__name__)


@dataclass
class Observation:
    key: str
    value: Any
    note: str = ""


@dataclass
class Hypothesis:
    claim: str
    confidence: float   # 0–1
    supporting: list[str] = field(default_factory=list)
    refuting: list[str] = field(default_factory=list)


@dataclass
class ReasoningTrace:
    task_id: str
    observations: list[Observation] = field(default_factory=list)
    hypotheses: list[Hypothesis] = field(default_factory=list)
    conclusion: str = ""
    chosen_worker_id: str = ""
    confidence: float = 0.0
    cot_text: str = ""


class ReasoningEngine:
    """
    Produces a ReasoningTrace explaining why a particular worker was chosen.
    Does not make the final selection — receives pre-ranked scores.
    """

    def reason(
        self,
        requirements: TaskRequirements,
        profiles: list[CapabilityProfile],
        scores: list[CapabilityScore],
    ) -> ReasoningTrace:
        trace = ReasoningTrace(task_id=requirements.task_id)

        # Observations
        trace.observations.append(Observation("candidate_count", len(profiles)))
        trace.observations.append(Observation("eligible_count", len(scores)))
        trace.observations.append(Observation("requires_gpu", requirements.requires_gpu))
        if requirements.required_skills:
            trace.observations.append(
                Observation("required_skills", sorted(requirements.required_skills))
            )
        if requirements.max_latency_ms < float("inf"):
            trace.observations.append(
                Observation("max_latency_ms", requirements.max_latency_ms)
            )

        if not scores:
            trace.conclusion = "No eligible workers found."
            trace.cot_text = ChainOfThought.format(trace)
            return trace

        best = scores[0]

        # Hypotheses
        trace.hypotheses.append(Hypothesis(
            claim=f"Worker {best.worker_id} is the best candidate",
            confidence=best.total_score,
            supporting=self._build_supporting(best, requirements),
        ))

        if len(scores) > 1:
            second = scores[1]
            gap = best.total_score - second.total_score
            trace.hypotheses.append(Hypothesis(
                claim=f"Worker {second.worker_id} is an acceptable fallback (gap={gap:.3f})",
                confidence=second.total_score,
                supporting=[f"score={second.total_score:.3f}"],
            ))

        trace.chosen_worker_id = best.worker_id
        trace.confidence = best.total_score
        trace.conclusion = (
            f"Selected {best.worker_id} (score={best.total_score:.3f}) "
            f"from {len(scores)} eligible workers. {best.explanation}"
        )
        trace.cot_text = ChainOfThought.format(trace)

        logger.debug("ReasoningEngine: %s → %s (confidence=%.3f)",
                     requirements.task_id, best.worker_id, trace.confidence)
        return trace

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_supporting(score: CapabilityScore, req: TaskRequirements) -> list[str]:
        parts = [f"total_score={score.total_score:.3f}"]
        if score.capability_score > 0.8:
            parts.append("strong capability match")
        if score.trust_score > 0.8:
            parts.append(f"high trust={score.trust_score:.2f}")
        if score.load_score > 0.7:
            parts.append("low current load")
        if req.requires_gpu and score.capability_score > 0:
            parts.append("GPU available")
        return parts


class ChainOfThought:
    """Renders a ReasoningTrace as a readable CoT paragraph."""

    @staticmethod
    def format(trace: ReasoningTrace) -> str:
        lines: list[str] = [f"# Reasoning for task {trace.task_id}"]

        if trace.observations:
            lines.append("\n## Observations")
            for obs in trace.observations:
                lines.append(f"- {obs.key}: {obs.value}" + (f" ({obs.note})" if obs.note else ""))

        if trace.hypotheses:
            lines.append("\n## Hypotheses")
            for h in trace.hypotheses:
                lines.append(f"- [{h.confidence:.2f}] {h.claim}")
                if h.supporting:
                    lines.append(f"  Supporting: {', '.join(h.supporting)}")
                if h.refuting:
                    lines.append(f"  Refuting: {', '.join(h.refuting)}")

        lines.append(f"\n## Conclusion\n{trace.conclusion}")
        return "\n".join(lines)
