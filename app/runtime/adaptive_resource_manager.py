"""
Wave 9B.4.3 — Adaptive Resource Manager

Continuously monitors worker state and makes runtime decisions:
  - scale recommendations (scale_up / scale_down)
  - task migration recommendations
  - queue rebalancing
  - workload throttling

ResourceSnapshot  — point-in-time worker state
ResourceDecision  — ARM recommendation
AdaptiveResourceManager — evaluation engine
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.runtime_intelligence.contracts import CapabilityProfile
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


class ResourceAction(str, Enum):
    SCALE_UP          = "scale_up"
    SCALE_DOWN        = "scale_down"
    MIGRATE_TASK      = "migrate_task"
    REBALANCE_QUEUE   = "rebalance_queue"
    THROTTLE          = "throttle"
    NO_ACTION         = "no_action"


@dataclass
class ResourceSnapshot:
    worker_id: str
    cpu_utilization: float = 0.0     # 0–1
    memory_utilization: float = 0.0  # 0–1
    gpu_utilization: float = 0.0     # 0–1
    queue_depth: int = 0
    in_flight_tasks: int = 0
    avg_latency_ms: float = 0.0
    token_budget_remaining: float = float("inf")
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ResourceDecision:
    action: ResourceAction
    target_worker_id: str = ""
    reason: str = ""
    urgency: float = 0.0    # 0–1


class AdaptiveResourceManager:
    """
    Evaluates worker resource snapshots and emits scaling / migration decisions.

    Thresholds (configurable):
      load_high_threshold   = 0.85  → scale_up recommendation
      load_low_threshold    = 0.15  → scale_down recommendation
      queue_overflow        = 64    → rebalance recommendation
      latency_spike_factor  = 2.0   → migrate if latency 2× baseline
    """

    def __init__(
        self,
        telemetry_bus: TelemetryBus | None = None,
        load_high_threshold: float = 0.85,
        load_low_threshold: float = 0.15,
        queue_overflow: int = 64,
        latency_spike_factor: float = 2.0,
    ) -> None:
        self._bus = telemetry_bus
        self._load_high = load_high_threshold
        self._load_low = load_low_threshold
        self._queue_overflow = queue_overflow
        self._latency_spike = latency_spike_factor
        self._baselines: dict[str, float] = {}   # worker_id → baseline latency

    def evaluate(
        self,
        snapshots: list[ResourceSnapshot],
        profiles: list[CapabilityProfile],
    ) -> list[ResourceDecision]:
        decisions: list[ResourceDecision] = []
        profile_map = {p.worker_id: p for p in profiles}

        for snap in snapshots:
            decision = self._evaluate_one(snap, profile_map.get(snap.worker_id))
            if decision.action != ResourceAction.NO_ACTION:
                decisions.append(decision)
                if self._bus:
                    if decision.action == ResourceAction.SCALE_UP:
                        et = TelemetryEventType.AUTOSCALE_TRIGGERED
                    elif decision.action == ResourceAction.THROTTLE:
                        et = TelemetryEventType.RESOURCE_PRESSURE
                    else:
                        et = TelemetryEventType.RESOURCE_PRESSURE
                    self._bus.emit(TelemetryEvent(
                        event_type=et,
                        source="AdaptiveResourceManager",
                        payload={
                            "action": decision.action,
                            "worker_id": snap.worker_id,
                            "reason": decision.reason,
                        },
                        worker_id=snap.worker_id,
                    ))

        return decisions

    def _evaluate_one(
        self,
        snap: ResourceSnapshot,
        profile: CapabilityProfile | None,
    ) -> ResourceDecision:
        # Update latency baseline (EMA)
        if snap.avg_latency_ms > 0:
            baseline = self._baselines.get(snap.worker_id, snap.avg_latency_ms)
            self._baselines[snap.worker_id] = 0.9 * baseline + 0.1 * snap.avg_latency_ms

        effective_load = max(snap.cpu_utilization, snap.memory_utilization)
        if profile:
            effective_load = max(effective_load, profile.current_load)

        # Queue overflow → rebalance
        if snap.queue_depth >= self._queue_overflow:
            return ResourceDecision(
                action=ResourceAction.REBALANCE_QUEUE,
                target_worker_id=snap.worker_id,
                reason=f"queue_depth={snap.queue_depth} ≥ {self._queue_overflow}",
                urgency=min(snap.queue_depth / self._queue_overflow, 1.0),
            )

        # High load → scale up
        if effective_load >= self._load_high:
            return ResourceDecision(
                action=ResourceAction.SCALE_UP,
                target_worker_id=snap.worker_id,
                reason=f"load={effective_load:.2f} ≥ {self._load_high}",
                urgency=effective_load,
            )

        # Latency spike → migrate
        baseline = self._baselines.get(snap.worker_id)
        if (baseline and snap.avg_latency_ms > 0
                and snap.avg_latency_ms > baseline * self._latency_spike):
            return ResourceDecision(
                action=ResourceAction.MIGRATE_TASK,
                target_worker_id=snap.worker_id,
                reason=(
                    f"latency={snap.avg_latency_ms:.0f}ms is "
                    f"{snap.avg_latency_ms / baseline:.1f}× baseline"
                ),
                urgency=min(snap.avg_latency_ms / (baseline * self._latency_spike), 1.0),
            )

        # Low load → scale down
        if effective_load <= self._load_low and snap.queue_depth == 0:
            return ResourceDecision(
                action=ResourceAction.SCALE_DOWN,
                target_worker_id=snap.worker_id,
                reason=f"load={effective_load:.2f} ≤ {self._load_low}",
                urgency=1.0 - effective_load,
            )

        return ResourceDecision(action=ResourceAction.NO_ACTION)
