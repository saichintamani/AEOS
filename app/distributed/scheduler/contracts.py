"""
Scheduler contracts — WorkerView, SchedulingRequest, SchedulingDecision, SchedulingStrategy.

WorkerView is a lightweight immutable struct used by strategies for pure placement
decisions. WorkerSnapshot (in pool/metrics.py) is the richer live record.

load_score formula: 40% task ratio + 30% CPU + 20% memory + 10% Kafka lag.

Contract: AC-SCHED-001
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerView:
    """Immutable scheduling-time view of a worker node."""
    node_id: str
    host: str = "127.0.0.1"
    region: str = "us-east-1"
    az: str = "a"

    in_flight_tasks: int = 0
    queue_depth: int = 0
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    gpu_utilization: float = 0.0
    kafka_consumer_lag: int = 0
    network_latency_ms: float = 0.0

    capabilities: frozenset[str] = field(default_factory=frozenset)
    max_in_flight: int = 16
    is_healthy: bool = True
    missed_heartbeats: int = 0

    @property
    def load_score(self) -> float:
        task_ratio = self.in_flight_tasks / max(self.max_in_flight, 1)
        lag_score = min(self.kafka_consumer_lag / 1000.0, 1.0)
        return (
            0.4 * task_ratio
            + 0.3 * self.cpu_utilization
            + 0.2 * self.memory_utilization
            + 0.1 * lag_score
        )

    @property
    def has_capacity(self) -> bool:
        return self.is_healthy and self.in_flight_tasks < self.max_in_flight


@dataclass
class SchedulingRequest:
    """Input to a scheduling decision."""
    task_id: str = ""
    workflow_id: str = ""
    step_id: str = ""
    task_type: str = ""
    priority: str = "normal"
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    preferred_region: str = ""
    preferred_az: str = ""
    affinity_node_id: str = ""
    anti_affinity_node_id: str = ""
    estimated_cpu_seconds: float = 0.0
    estimated_memory_mb: float = 0.0
    estimated_gpu_seconds: float = 0.0


@dataclass
class SchedulingDecision:
    """Result of a scheduling strategy selection."""
    worker: WorkerView
    strategy_name: str
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class SchedulingError(Exception):
    """Raised when no suitable worker can be found."""


class SchedulingStrategy(ABC):
    """
    Strategy Pattern: pluggable placement algorithm.

    All strategies receive the full list of workers and a SchedulingRequest.
    The DistributedScheduler swaps strategies at runtime via set_strategy().
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier for logging and metrics."""

    @abstractmethod
    def select(
        self,
        workers: list[WorkerView],
        request: SchedulingRequest,
    ) -> SchedulingDecision | None:
        """Select the best worker. Returns None if no suitable worker exists."""

    def filter_healthy(self, workers: list[WorkerView]) -> list[WorkerView]:
        return [w for w in workers if w.has_capacity]
