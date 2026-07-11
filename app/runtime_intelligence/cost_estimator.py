"""
Wave 9B.3.7 — Cost Estimator

Estimates the cost of running a task on a given worker, combining:
  - Token cost (token_cost_per_k * estimated_tokens)
  - Compute cost (compute_cost_per_hour * estimated_duration_hours)
"""

from __future__ import annotations

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements

# Default assumed token count when not specified in payload
_DEFAULT_TOKENS = 2000


class CostEstimator:
    def estimate(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
        estimated_duration_ms: float = 500.0,
    ) -> float:
        tokens = requirements.payload.get("estimated_tokens", _DEFAULT_TOKENS)
        token_cost = profile.token_cost_per_k * (tokens / 1000.0)

        duration_hours = estimated_duration_ms / 3_600_000.0
        compute_cost = profile.compute_cost_per_hour * duration_hours

        return round(token_cost + compute_cost, 6)
