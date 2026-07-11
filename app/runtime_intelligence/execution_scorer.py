"""
Wave 9B.3.7 — Execution Scorer

Combines all scoring signals into a single expected utility value,
including predictions from the learning engine.
"""

from __future__ import annotations

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    CapabilityScore,
    TaskRequirements,
)
from app.runtime_intelligence.cost_estimator import CostEstimator
from app.runtime_intelligence.learning_engine import DefaultLearningEngine

_PRED_WEIGHT = 0.15   # weight given to historical success rate prediction
_CAP_WEIGHT  = 0.85   # weight given to capability score


class ExecutionScorer:
    """
    Enriches a CapabilityScore with predicted success rate from the
    LearningEngine to produce a final expected utility.
    """

    def __init__(self, learning_engine: DefaultLearningEngine) -> None:
        self._engine = learning_engine
        self._cost_estimator = CostEstimator()

    async def score(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
        base_score: CapabilityScore,
        estimated_duration_ms: float = 500.0,
    ) -> float:
        predicted_sr = await self._engine.predict_success_rate(
            profile.worker_id, requirements.task_type
        )
        utility = _CAP_WEIGHT * base_score.total_score + _PRED_WEIGHT * predicted_sr
        return round(min(utility, 1.0), 4)

    def estimate_cost(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
        estimated_duration_ms: float = 500.0,
    ) -> float:
        return self._cost_estimator.estimate(profile, requirements, estimated_duration_ms)
