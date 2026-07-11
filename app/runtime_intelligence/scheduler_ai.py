"""
Wave 9B.3.4 — Multi-objective Adaptive Scheduler (AI Scheduler)

Ties together:
  StrategySelector → selects scheduling strategy
  CapabilityRanker → scores and ranks workers
  PolicyRanker     → applies governance constraints
  ReasoningEngine  → generates trace for explainability
  DecisionEngine   → wraps everything in an ExecutionDecision

The AIScheduler is the single entry point for "pick the best worker for this task."
"""

from __future__ import annotations

import logging

from app.runtime_intelligence.capability_matcher import (
    CapabilityRanker,
    DefaultCapabilityMatcher,
)
from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    DecisionDimension,
    DecisionEngine,
    ExecutionDecision,
    TaskRequirements,
)
from app.runtime_intelligence.policy_ranker import PolicyRanker
from app.runtime_intelligence.reasoning import ReasoningEngine
from app.runtime_intelligence.strategy_selector import StrategySelector

logger = logging.getLogger(__name__)


class AIScheduler(DecisionEngine):
    """
    Multi-objective adaptive scheduler.

    Pipeline:
      1. StrategySelector picks the scheduling strategy
      2. CapabilityRanker scores and ranks workers
      3. PolicyRanker applies governance policy
      4. ReasoningEngine produces a trace
      5. ExecutionDecision wraps the result
    """

    def __init__(
        self,
        strategy_selector: StrategySelector | None = None,
        policy_ranker: PolicyRanker | None = None,
        reasoning_engine: ReasoningEngine | None = None,
    ) -> None:
        self._ranker = CapabilityRanker(DefaultCapabilityMatcher())
        self._strategy_selector = strategy_selector or StrategySelector()
        self._policy_ranker = policy_ranker or PolicyRanker()
        self._reasoning = reasoning_engine or ReasoningEngine()

    async def decide(
        self,
        requirements: TaskRequirements,
        profiles: list[CapabilityProfile],
    ) -> ExecutionDecision:
        strategy = self._strategy_selector.select(requirements)

        # Rank
        scores = self._ranker.rank(profiles, requirements)

        # Policy filter
        profile_map = {p.worker_id: p for p in profiles}
        scores = self._policy_ranker.apply(scores, profile_map, requirements)
        scores = [s for s in scores if s.total_score > 0]

        # Reason
        trace = self._reasoning.reason(requirements, profiles, scores)

        if not scores:
            return ExecutionDecision(
                task_id=requirements.task_id,
                worker_id="",
                strategy_name=strategy,
                expected_utility=0.0,
                explanation="No eligible workers after policy filtering.",
                confidence=0.0,
            )

        best = scores[0]
        alternatives = [s.worker_id for s in scores[1:6]]

        dimensions = [
            DecisionDimension("capability", best.capability_score, 0.25, "skills/models/GPU match"),
            DecisionDimension("trust",      best.trust_score,       0.20, "governance trust score"),
            DecisionDimension("load",       best.load_score,        0.20, "inverse current load"),
            DecisionDimension("latency",    best.latency_score,     0.15, "latency constraint fit"),
            DecisionDimension("cost",       best.cost_score,        0.10, "cost constraint fit"),
            DecisionDimension("locality",   best.locality_score,    0.10, "region/AZ affinity"),
        ]

        decision = ExecutionDecision(
            task_id=requirements.task_id,
            worker_id=best.worker_id,
            strategy_name=strategy,
            expected_utility=best.total_score,
            dimensions=dimensions,
            explanation=trace.conclusion,
            alternatives=alternatives,
            confidence=trace.confidence,
        )

        logger.info(
            "AIScheduler[%s]: task %s → worker %s (utility=%.3f, confidence=%.3f)",
            strategy, requirements.task_id, best.worker_id,
            best.total_score, trace.confidence,
        )
        return decision
