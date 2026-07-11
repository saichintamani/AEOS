"""
Wave 9B.3.6 — Runtime Predictor

Uses the LearningEngine's historical data to predict:
  - Execution time for a (worker, task_type) pair
  - Likely cost for a task
  - Whether a deadline will be met
"""

from __future__ import annotations

import logging

from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    TaskRequirements,
)
from app.runtime_intelligence.learning_engine import DefaultLearningEngine

logger = logging.getLogger(__name__)

# Fallback estimates when no history exists
_DEFAULT_LATENCY_MS = 500.0
_DEFAULT_COST = 0.01


class RuntimePredictor:
    """
    Predicts runtime characteristics for task scheduling decisions.
    Backed by a DefaultLearningEngine instance.
    """

    def __init__(self, learning_engine: DefaultLearningEngine) -> None:
        self._engine = learning_engine

    async def predict_latency_ms(
        self, worker_id: str, task_type: str
    ) -> float:
        stats_list = await self._engine.worker_stats(worker_id)
        for s in stats_list:
            if s.task_type == task_type and s.ema_latency_ms > 0:
                return s.ema_latency_ms
        return _DEFAULT_LATENCY_MS

    async def predict_cost(self, worker_id: str, task_type: str) -> float:
        stats_list = await self._engine.worker_stats(worker_id)
        for s in stats_list:
            if s.task_type == task_type and s.ema_cost > 0:
                return s.ema_cost
        return _DEFAULT_COST

    async def will_meet_deadline(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
    ) -> bool:
        """Returns True if the predicted latency is within max_latency_ms."""
        if requirements.max_latency_ms == float("inf"):
            return True
        predicted = await self.predict_latency_ms(profile.worker_id, requirements.task_type)
        return predicted <= requirements.max_latency_ms

    async def annotate_decision(
        self,
        worker_id: str,
        requirements: TaskRequirements,
    ) -> dict:
        """Returns a dict of predicted metrics to attach to ExecutionDecision."""
        latency = await self.predict_latency_ms(worker_id, requirements.task_type)
        cost = await self.predict_cost(worker_id, requirements.task_type)
        success_rate = await self._engine.predict_success_rate(worker_id, requirements.task_type)
        return {
            "predicted_latency_ms": round(latency, 2),
            "predicted_cost": round(cost, 4),
            "predicted_success_rate": round(success_rate, 4),
        }
