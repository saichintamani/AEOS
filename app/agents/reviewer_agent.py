"""
AEOS Reviewer Agent — v2 CognitiveAgent
Validates agent outputs and issues PASS | REVISE | REJECT verdicts.
Acts as the quality gate in the multi-agent pipeline.

Migrated from v1 think()/act() to v2 11-step CognitiveAgent runtime.
_step_execute runs scoring and verdict; _step_reflect is overridden to
propagate the review verdict into the cognitive reflection artifact.
"""

from __future__ import annotations
from typing import Any

from app.agents.cognitive import (
    CognitiveAgent, CognitiveContext,
    ExecutionResult, ReflectionResult,
)
from app.core.logger import get_logger

log = get_logger(__name__)

_PASS_THRESHOLD   = 0.70
_REVISE_THRESHOLD = 0.40


class ReviewerAgent(CognitiveAgent):

    def __init__(self) -> None:
        super().__init__()
        self.id = "reviewer_agent"
        self.name = "Output Reviewer Agent"
        self.capabilities = [
            "output_validation",
            "quality_scoring",
            "revision_flagging",
            "confidence_assessment",
        ]

    async def initialize(self) -> None:
        await super().initialize()

    # ── Step 8: Execute — scoring and verdict ──────────────────────────────────

    async def _step_execute(self, ctx: CognitiveContext) -> None:
        # The result to review comes from upstream pipeline nodes or context
        upstream = ctx.raw_context.get("upstream_results", {})
        result = ctx.raw_context.get("previous_result") or (
            next(iter(upstream.values()), {}) if upstream else {}
        )
        revision_round = ctx.raw_context.get("revision_round", 0)

        scores = {
            "completeness": self._score_completeness(result),
            "confidence":   self._score_confidence(result),
            "structure":    self._score_structure(result),
        }
        overall = round(
            0.4 * scores["completeness"] + 0.4 * scores["confidence"] + 0.2 * scores["structure"],
            3,
        )
        scores["overall"] = overall

        verdict  = self._determine_verdict(overall, revision_round)
        feedback = self._generate_feedback(verdict, scores)
        hints    = self._revision_hints(verdict, scores, result)

        self._log.info(
            "Review complete",
            extra={"ctx_verdict": verdict, "ctx_overall": overall},
        )

        ctx.execution = ExecutionResult(
            success=True,
            output={
                "verdict": verdict,
                "scores": scores,
                "feedback": feedback,
                "revision_hints": hints,
                "revision_round": revision_round,
            },
        )

    # ── Step 9: Reflect — map review verdict to ReflectionResult ──────────────

    async def _step_reflect(self, ctx: CognitiveContext) -> None:
        if ctx.execution is None or not ctx.execution.output:
            await super()._step_reflect(ctx)
            return

        review = ctx.execution.output
        overall = review.get("scores", {}).get("overall", 0.5)
        verdict = review.get("verdict", "PASS")

        ctx.reflection = ReflectionResult(
            quality_score=overall,
            completeness=review.get("scores", {}).get("completeness", overall),
            accuracy=review.get("scores", {}).get("confidence", overall),
            passes_criteria=verdict == "PASS",
            needs_revision=verdict == "REVISE",
            revision_suggestions=review.get("revision_hints", []),
        )

    # ── Scoring ────────────────────────────────────────────────────────────────

    def _score_completeness(self, result: Any) -> float:
        if result is None:
            return 0.0
        if isinstance(result, dict):
            if not result:
                return 0.1
            filled = sum(1 for v in result.values() if v not in (None, "", [], {}))
            return round(filled / max(len(result), 1), 2)
        if isinstance(result, str):
            return min(len(result) / 200, 1.0)
        if isinstance(result, list):
            return min(len(result) / 3, 1.0)
        return 0.5

    def _score_confidence(self, result: Any) -> float:
        if isinstance(result, dict):
            for key in ("confidence", "score", "certainty", "probability"):
                if key in result and isinstance(result[key], (int, float)):
                    return float(result[key])
            if "step_results" in result:
                confs = []
                for v in result["step_results"].values():
                    if isinstance(v, dict):
                        for key in ("confidence", "score"):
                            if key in v:
                                confs.append(float(v[key]))
                if confs:
                    return round(sum(confs) / len(confs), 2)
            good_keys = {"summary", "result", "findings", "analysis", "recommendation"}
            found = good_keys.intersection(result.keys())
            return round(0.4 + 0.1 * len(found), 2)
        return 0.5

    def _score_structure(self, result: Any) -> float:
        if result is None:
            return 0.0
        if isinstance(result, dict) and len(result) >= 2:
            return 0.9
        if isinstance(result, (list, str)) and result:
            return 0.7
        return 0.3

    # ── Verdict ────────────────────────────────────────────────────────────────

    def _determine_verdict(self, overall: float, revision_round: int) -> str:
        if revision_round >= 2 and overall >= _REVISE_THRESHOLD:
            return "PASS"
        if overall >= _PASS_THRESHOLD:
            return "PASS"
        if overall >= _REVISE_THRESHOLD:
            return "REVISE"
        return "REJECT"

    def _generate_feedback(self, verdict: str, scores: dict) -> str:
        if verdict == "PASS":
            return f"Output meets quality standards. Overall score: {scores['overall']:.2f}."
        if verdict == "REVISE":
            weak = [k for k, v in scores.items() if k != "overall" and v < 0.6]
            return f"Output needs improvement in: {', '.join(weak)}. Overall score: {scores['overall']:.2f}."
        return f"Output rejected — quality below acceptable threshold ({scores['overall']:.2f}). Recommend full re-execution."

    def _revision_hints(self, verdict: str, scores: dict, result: Any) -> list[str]:
        if verdict == "PASS":
            return []
        hints = []
        if scores["completeness"] < 0.6:
            hints.append("Ensure all required output fields are populated with meaningful values.")
        if scores["confidence"] < 0.6:
            hints.append("Include explicit confidence scores or certainty indicators in the output.")
        if scores["structure"] < 0.6:
            hints.append("Return a structured dict with clearly named fields rather than raw text.")
        if not hints:
            hints.append("Improve response depth and coverage of the original task.")
        return hints
