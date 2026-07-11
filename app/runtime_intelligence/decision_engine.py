"""
Wave 9B.3.7 — Expected Utility Decision Engine

The full Decision Engine that integrates:
  CapabilityRanker → base scoring
  PolicyRanker     → governance constraints
  ExecutionScorer  → learning-enhanced utility
  ConfidenceCalc   → confidence score
  RuntimePredictor → latency/cost predictions
  ReasoningEngine  → CoT trace
  StrategySelector → strategy name

Returns a fully explained ExecutionDecision with all signals combined.
"""

from __future__ import annotations

import logging

from app.runtime_intelligence.capability_matcher import (
    CapabilityRanker,
    DefaultCapabilityMatcher,
)
from app.runtime_intelligence.confidence import ConfidenceCalculator
from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    DecisionDimension,
    DecisionEngine,
    ExecutionDecision,
    TaskRequirements,
)
from app.runtime_intelligence.execution_scorer import ExecutionScorer
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime_intelligence.policy_ranker import PolicyRanker
from app.runtime_intelligence.reasoning import ReasoningEngine
from app.runtime_intelligence.runtime_predictor import RuntimePredictor
from app.runtime_intelligence.strategy_selector import StrategySelector

logger = logging.getLogger(__name__)


class ExpectedUtilityDecisionEngine(DecisionEngine):
    """
    Production-grade decision engine that uses expected utility theory.

    Expected Utility = weighted sum of:
      - Capability match (with learning-enhanced success prediction)
      - Trust (governance)
      - Load (inverse of current load)
      - Latency fit (vs constraint)
      - Cost fit (vs budget)
      - Locality affinity
    """

    def __init__(
        self,
        learning_engine: DefaultLearningEngine | None = None,
        policy_ranker: PolicyRanker | None = None,
        reasoning_engine: ReasoningEngine | None = None,
        strategy_selector: StrategySelector | None = None,
    ) -> None:
        self._learning = learning_engine or DefaultLearningEngine()
        self._ranker = CapabilityRanker(DefaultCapabilityMatcher())
        self._policy = policy_ranker or PolicyRanker()
        self._scorer = ExecutionScorer(self._learning)
        self._confidence = ConfidenceCalculator()
        self._predictor = RuntimePredictor(self._learning)
        self._reasoning = reasoning_engine or ReasoningEngine()
        self._strategy_selector = strategy_selector or StrategySelector()

    async def decide(
        self,
        requirements: TaskRequirements,
        profiles: list[CapabilityProfile],
    ) -> ExecutionDecision:
        strategy = self._strategy_selector.select(requirements)

        # Base ranking
        scores = self._ranker.rank(profiles, requirements)

        # Policy filter
        profile_map = {p.worker_id: p for p in profiles}
        scores = self._policy.apply(scores, profile_map, requirements)
        scores = [s for s in scores if s.total_score > 0]

        if not scores:
            return ExecutionDecision(
                task_id=requirements.task_id,
                worker_id="",
                strategy_name=strategy,
                expected_utility=0.0,
                explanation="No eligible workers after policy filtering.",
                confidence=0.0,
            )

        # Enhance top-N with learning predictions (max 5 candidates)
        enriched = []
        for score in scores[:5]:
            profile = profile_map.get(score.worker_id)
            if profile:
                utility = await self._scorer.score(profile, requirements, score)
                score.total_score = utility
                enriched.append(score)
            else:
                enriched.append(score)

        enriched.sort(key=lambda s: s.total_score, reverse=True)

        best = enriched[0]
        best_profile = profile_map.get(best.worker_id)
        alternatives = [s.worker_id for s in enriched[1:]]

        # Confidence
        hist_records = 0
        if best_profile:
            stats = await self._learning.worker_stats(best.worker_id)
            for s in stats:
                if s.task_type == requirements.task_type:
                    hist_records = s.total
                    break

        confidence = self._confidence.calculate(enriched, hist_records)

        # Predictions
        predictions = {}
        if best_profile:
            predictions = await self._predictor.annotate_decision(best.worker_id, requirements)

        # Reasoning trace
        trace = self._reasoning.reason(requirements, profiles, enriched)

        dimensions = [
            DecisionDimension("capability", best.capability_score, 0.25),
            DecisionDimension("trust",      best.trust_score,       0.20),
            DecisionDimension("load",       best.load_score,        0.20),
            DecisionDimension("latency",    best.latency_score,     0.15),
            DecisionDimension("cost",       best.cost_score,        0.10),
            DecisionDimension("locality",   best.locality_score,    0.10),
        ]

        decision = ExecutionDecision(
            task_id=requirements.task_id,
            worker_id=best.worker_id,
            strategy_name=strategy,
            expected_utility=best.total_score,
            dimensions=dimensions,
            explanation=trace.conclusion,
            alternatives=alternatives,
            confidence=confidence,
            cost_estimate=predictions.get("predicted_cost", 0.0),
            latency_estimate_ms=predictions.get("predicted_latency_ms", 0.0),
        )

        logger.info(
            "DecisionEngine[%s]: task=%s worker=%s utility=%.3f confidence=%.3f",
            strategy, requirements.task_id, best.worker_id,
            best.total_score, confidence,
        )
        return decision
