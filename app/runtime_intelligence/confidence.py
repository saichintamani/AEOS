"""
Wave 9B.3.7 — Confidence Calculator

Produces a confidence score [0,1] for an ExecutionDecision based on:
  - Score gap between best and second-best candidate
  - Historical data coverage for this worker/task_type pair
  - Whether hard constraints were barely satisfied
"""

from __future__ import annotations

from app.runtime_intelligence.contracts import CapabilityScore

_MIN_GAP_FOR_HIGH_CONFIDENCE = 0.10
_MIN_RECORDS_FOR_FULL_CONFIDENCE = 20


class ConfidenceCalculator:
    """
    Computes decision confidence given ranked scores and historical record count.
    """

    def calculate(
        self,
        scores: list[CapabilityScore],
        historical_records: int = 0,
    ) -> float:
        if not scores:
            return 0.0

        best = scores[0].total_score

        # Gap penalty: small gap → lower confidence
        if len(scores) > 1:
            gap = best - scores[1].total_score
            gap_factor = min(gap / _MIN_GAP_FOR_HIGH_CONFIDENCE, 1.0)
        else:
            gap_factor = 1.0   # only one candidate = certain

        # Data coverage: more history → higher confidence
        coverage = min(historical_records / _MIN_RECORDS_FOR_FULL_CONFIDENCE, 1.0)
        # Blend: if no history, lean on gap_factor; with history, blend equally
        data_weight = 0.3 + 0.2 * coverage   # 0.3 → 0.5
        gap_weight = 1.0 - data_weight

        confidence = gap_weight * gap_factor * best + data_weight * coverage
        return round(min(confidence, 1.0), 4)
