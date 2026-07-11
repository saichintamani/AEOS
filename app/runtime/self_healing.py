"""
Wave 9B.4.4 — Self-Healing Runtime

When a worker crashes or an execution fails, SelfHealingRuntime:
  1. Detects the failure (via heartbeat timeout or explicit event)
  2. Analyzes available checkpoint
  3. Selects a recovery target worker
  4. Migrates execution state
  5. Updates reliability metrics in the LearningEngine
  6. Emits telemetry

HealingAction    — describes what recovery was performed
SelfHealingRuntime — coordinates the full recovery flow
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from app.runtime_intelligence.capability_matcher import CapabilityResolver
from app.runtime_intelligence.contracts import (
    CapabilityProfile,
    ExecutionRecord,
    TaskRequirements,
)
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


class RecoveryStrategy(str, Enum):
    REQUEUE          = "requeue"          # no checkpoint — restart from scratch
    RESUME           = "resume"           # committed checkpoint found — resume from it
    MIGRATE          = "migrate"          # move to a different worker
    ESCALATE         = "escalate"         # max retries exceeded
    NO_ACTION        = "no_action"


@dataclass
class FailureContext:
    task_id: str
    worker_id: str
    task_type: str
    workflow_id: str = ""
    error_type: str = ""
    attempt: int = 0
    max_attempts: int = 3
    has_checkpoint: bool = False
    checkpoint_committed: bool = False
    checkpoint_age_ms: float = 0.0     # staleness


@dataclass
class HealingAction:
    strategy: RecoveryStrategy
    task_id: str
    failed_worker_id: str
    target_worker_id: str = ""
    resume_from_checkpoint: bool = False
    explanation: str = ""
    metrics_updated: bool = False


class SelfHealingRuntime:
    """
    Coordinates recovery from execution failures.

    Recovery decision tree:
      1. attempt >= max_attempts  → ESCALATE
      2. committed checkpoint      → RESUME on best available worker
      3. no checkpoint             → REQUEUE (restart)
    """

    def __init__(
        self,
        learning_engine: DefaultLearningEngine | None = None,
        telemetry_bus: TelemetryBus | None = None,
    ) -> None:
        self._learning = learning_engine or DefaultLearningEngine()
        self._resolver = CapabilityResolver()
        self._bus = telemetry_bus

    async def heal(
        self,
        failure: FailureContext,
        available_workers: list[CapabilityProfile],
    ) -> HealingAction:
        action = await self._decide(failure, available_workers)
        await self._update_metrics(failure)
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.EXECUTION_RECOVERED,
                source="SelfHealingRuntime",
                payload={
                    "strategy": action.strategy,
                    "task_id": failure.task_id,
                    "failed_worker": failure.worker_id,
                    "target_worker": action.target_worker_id,
                    "explanation": action.explanation,
                },
                correlation_id=failure.task_id,
                worker_id=failure.worker_id,
            ))
        logger.info(
            "SelfHealingRuntime: task=%s strategy=%s failed_on=%s → %s",
            failure.task_id, action.strategy, failure.worker_id, action.target_worker_id,
        )
        return action

    # ── Decision ──────────────────────────────────────────────────────────────

    async def _decide(
        self,
        failure: FailureContext,
        workers: list[CapabilityProfile],
    ) -> HealingAction:
        if failure.attempt >= failure.max_attempts:
            return HealingAction(
                strategy=RecoveryStrategy.ESCALATE,
                task_id=failure.task_id,
                failed_worker_id=failure.worker_id,
                explanation=(
                    f"Max attempts ({failure.max_attempts}) exceeded — escalating to dead-letter queue."
                ),
            )

        # Find best available worker (excluding the failed one)
        candidates = [w for w in workers if w.worker_id != failure.worker_id and w.is_healthy]
        target = None
        if candidates:
            req = TaskRequirements(
                task_id=failure.task_id,
                task_type=failure.task_type,
                workflow_id=failure.workflow_id,
            )
            score = self._resolver.resolve(candidates, req)
            if score:
                target = score.worker_id

        if failure.has_checkpoint and failure.checkpoint_committed:
            return HealingAction(
                strategy=RecoveryStrategy.RESUME,
                task_id=failure.task_id,
                failed_worker_id=failure.worker_id,
                target_worker_id=target or "",
                resume_from_checkpoint=True,
                explanation=(
                    f"Committed checkpoint found — resuming on {target or 'same worker'}."
                ),
            )

        if target:
            return HealingAction(
                strategy=RecoveryStrategy.MIGRATE,
                task_id=failure.task_id,
                failed_worker_id=failure.worker_id,
                target_worker_id=target,
                resume_from_checkpoint=False,
                explanation=f"Migrating to {target} — no usable checkpoint, restarting.",
            )

        return HealingAction(
            strategy=RecoveryStrategy.REQUEUE,
            task_id=failure.task_id,
            failed_worker_id=failure.worker_id,
            explanation="No healthy alternative workers — requeuing on same worker when available.",
        )

    async def _update_metrics(self, failure: FailureContext) -> None:
        record = ExecutionRecord(
            task_id=failure.task_id,
            worker_id=failure.worker_id,
            task_type=failure.task_type,
            workflow_id=failure.workflow_id,
            success=False,
            failed=True,
            error_type=failure.error_type,
            retries=failure.attempt,
        )
        await self._learning.record(record)
