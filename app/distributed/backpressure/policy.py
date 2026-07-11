"""
Threshold-based backpressure policy.

Evaluates worker pool metrics against configurable thresholds
and emits BackpressureAction recommendations.

Thresholds (defaults):
  queue_warn_threshold   = 32   → SLOW
  queue_reject_threshold = 56   → REJECT
  cpu_threshold          = 0.85 → ALERT
  memory_threshold       = 0.90 → ALERT
  min_healthy_workers    = 1    → REJECT if below
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.distributed.pool.metrics import WorkerSnapshot


class BackpressureAction(str, Enum):
    NONE   = "none"
    SLOW   = "slow"
    REJECT = "reject"
    SCALE  = "scale"
    ALERT  = "alert"


@dataclass
class ThresholdPolicy:
    queue_warn_threshold:   float = 32.0
    queue_reject_threshold: float = 56.0
    cpu_threshold:          float = 0.85
    memory_threshold:       float = 0.90
    min_healthy_workers:    int   = 1
    slow_delay:             float = 0.5   # seconds to delay when SLOWING

    def evaluate(self, snapshots: list["WorkerSnapshot"]) -> BackpressureAction:
        if not snapshots:
            return BackpressureAction.REJECT

        healthy = [s for s in snapshots if s.is_healthy]
        if len(healthy) < self.min_healthy_workers:
            return BackpressureAction.REJECT

        max_queue  = max((s.queue_depth for s in healthy), default=0)
        max_cpu    = max((s.cpu_utilization for s in healthy), default=0.0)
        max_memory = max((s.memory_utilization for s in healthy), default=0.0)

        if max_queue >= self.queue_reject_threshold:
            return BackpressureAction.REJECT
        if max_cpu >= self.cpu_threshold or max_memory >= self.memory_threshold:
            return BackpressureAction.ALERT
        if max_queue >= self.queue_warn_threshold:
            return BackpressureAction.SLOW
        return BackpressureAction.NONE
