"""
AEOS Analyst Agent — v2 CognitiveAgent
Structured reasoning engine: extracts facts from prior results,
forms hypotheses via pattern-matching, draws conclusions, and persists
the analysis to long-term memory for future pipeline steps.

Routing keyword triggers: analyze, evaluate, assess, compare, examine

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
_step_hypothesize overridden with domain-specific hypothesis generation.
_step_execute performs fact extraction, conclusion drawing, and memory write.
"""

from __future__ import annotations
import re
from typing import Any

from app.agents.cognitive import (
    CognitiveAgent, CognitiveContext,
    ExecutionResult, EvaluatedHypotheses, Hypothesis,
)
from app.core.logger import get_logger

log = get_logger(__name__)

_COMPARATIVE = {"compare", "versus", "vs", "difference", "contrast", "better", "worse"}
_CAUSAL      = {"why", "cause", "reason", "because", "leads to", "result", "impact"}
_STATISTICAL = {"count", "average", "mean", "distribution", "frequency", "rate", "percent"}
_EVALUATIVE  = {"evaluate", "assess", "judge", "rate", "score", "quality", "performance"}


class AnalystAgent(CognitiveAgent):
    """
    Structured reasoning agent.

    Pipeline:
        _step_hypothesize → domain-specific hypothesis generation
        _step_execute     → fact extraction → conclusions → memory write
    """

    def __init__(self) -> None:
        super().__init__()
        self.id = "analyst_agent"
        self.name = "Analyst Agent"
        self.capabilities = [
            "structured_reasoning",
            "fact_extraction",
            "hypothesis_generation",
            "conclusion_drawing",
            "data_interpretation",
        ]

    async def initialize(self) -> None:
        await super().initialize()
        self._log.debug("AnalystAgent ready")

    # ── Step 5: Hypothesize — domain-specific hypothesis generation ────────────

    async def _step_hypothesize(self, ctx: CognitiveContext) -> None:
        task = ctx.task
        previous = ctx.raw_context.get("previous_result") or {}
        upstream = ctx.raw_context.get("upstream_results", {})
        if not previous and upstream:
            previous = next(iter(upstream.values()), {})

        facts = self._extract_facts(previous, [])
        hypotheses = self._form_hypotheses(task, facts)

        ctx.hypotheses = EvaluatedHypotheses(
            hypotheses=[
                Hypothesis(
                    description=h,
                    approach="structured_analysis",
                    expected_outcome="Evidence-based conclusion",
                    feasibility=0.85,
                    confidence=0.7,
                )
                for h in hypotheses
            ]
        )
        if ctx.hypotheses.hypotheses:
            ctx.hypotheses.selected = ctx.hypotheses.hypotheses[0]
            ctx.hypotheses.selection_rationale = "Primary hypothesis selected for evaluation"

    # ── Step 8: Execute ────────────────────────────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        task = ctx.task
        previous = ctx.raw_context.get("previous_result") or {}
        upstream = ctx.raw_context.get("upstream_results", {})
        if not previous and upstream:
            previous = next(iter(upstream.values()), {})

        analysis_type = self._classify_analysis(task)
        facts = self._extract_facts(previous, [])
        hypotheses_text = [h.description for h in (ctx.hypotheses.hypotheses if ctx.hypotheses else [])]
        conclusions, confidence = self._draw_conclusions(facts, hypotheses_text)
        evidence_strength = self._rate_evidence(facts, conclusions)
        recommendation = self._recommend(conclusions, confidence)

        # Write conclusions to long-term memory
        try:
            from app.core.memory import get_memory
            memory = get_memory()
            memory.write_long(
                key=f"analysis:{task[:50]}",
                value={"conclusions": conclusions, "confidence": confidence},
                agent_id=self.id,
                task_id=ctx.cycle_id,
            )
        except Exception as exc:
            self._log.warning("Could not write to memory", extra={"ctx_error": str(exc)})

        ctx.execution = ExecutionResult(
            success=True,
            output={
                "analysis_type": analysis_type,
                "facts": facts,
                "hypotheses": hypotheses_text,
                "conclusions": conclusions,
                "confidence": round(confidence, 3),
                "evidence_strength": evidence_strength,
                "recommendation": recommendation,
            },
        )

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _classify_analysis(self, task: str) -> str:
        words = set(task.lower().split())
        if words & _COMPARATIVE:
            return "comparative"
        if words & _CAUSAL:
            return "causal"
        if words & _STATISTICAL:
            return "statistical"
        if words & _EVALUATIVE:
            return "evaluative"
        return "descriptive"

    def _estimate_complexity(self, task: str) -> int:
        length_score = min(len(task) // 40, 5)
        kw_score = min(len(re.findall(r"\b\w+\b", task)) // 8, 5)
        return max(1, min(10, length_score + kw_score))

    def _extract_facts(self, previous: Any, history: list[dict]) -> list[str]:
        facts: list[str] = []

        def _harvest(obj: Any, depth: int = 0) -> None:
            if depth > 3:
                return
            if isinstance(obj, str) and len(obj) > 10:
                facts.append(obj[:200])
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if isinstance(v, (str, int, float)) and str(v):
                        facts.append(f"{k}: {v}"[:200])
                    else:
                        _harvest(v, depth + 1)
            elif isinstance(obj, list):
                for item in obj[:5]:
                    _harvest(item, depth + 1)

        _harvest(previous)
        for h in history[-3:]:
            _harvest(h)

        seen: set[str] = set()
        unique: list[str] = []
        for f in facts:
            if f not in seen:
                seen.add(f)
                unique.append(f)
        return unique[:15]

    def _form_hypotheses(self, task: str, facts: list[str]) -> list[str]:
        hypotheses: list[str] = []
        task_lower = task.lower()

        if not facts:
            return ["Insufficient data to form specific hypotheses."]

        if any(w in task_lower for w in ("improve", "optimize", "better")):
            hypotheses.append("There are optimization opportunities in the current approach.")
        if any(w in task_lower for w in ("risk", "issue", "problem", "bug", "error")):
            hypotheses.append("The identified issues may have compounding effects if unaddressed.")
        if any(w in task_lower for w in ("compare", "vs", "versus")):
            hypotheses.append("The compared elements likely differ in non-obvious ways.")
        if len(facts) >= 3:
            hypotheses.append(
                f"The {len(facts)} data points suggest a pattern worth deeper investigation."
            )
        if not hypotheses:
            hypotheses.append(
                "The available evidence is consistent with multiple interpretations."
            )
        return hypotheses[:4]

    def _draw_conclusions(
        self, facts: list[str], hypotheses: list[str]
    ) -> tuple[list[str], float]:
        conclusions: list[str] = []

        if not facts:
            return ["Insufficient evidence to draw reliable conclusions."], 0.2

        conclusions.append(f"Analysis based on {len(facts)} extracted data points.")
        if len(facts) >= 5:
            conclusions.append("The volume of evidence supports moderate-to-high confidence.")
        if hypotheses and "insufficient" not in hypotheses[0].lower():
            conclusions.append(f"Primary hypothesis: {hypotheses[0]}")

        confidence = min(0.95, 0.3 + 0.05 * len(facts))
        return conclusions[:5], round(confidence, 3)

    def _rate_evidence(self, facts: list[str], conclusions: list[str]) -> str:
        n = len(facts)
        if n >= 8:
            return "strong"
        if n >= 3:
            return "moderate"
        return "weak"

    def _recommend(self, conclusions: list[str], confidence: float) -> str:
        if confidence >= 0.75:
            return "Proceed with high confidence — evidence strongly supports conclusions."
        if confidence >= 0.50:
            return "Proceed with caution — conclusions are plausible but not definitive."
        return "Gather more data before acting — current evidence is limited."
