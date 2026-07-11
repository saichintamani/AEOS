"""
Runtime metrics collector.

Pre-registers all standard AEOS metrics with the aeos_ prefix as defined
in §9.2 of the architecture spec. One RuntimeMetricsCollector per node.

Contract: AC-OBS-001
"""

from __future__ import annotations

import time

from app.distributed.metrics.registry import Counter, Gauge, Histogram, MetricsRegistry


class RuntimeMetricsCollector:
    """
    Convenience wrapper that pre-registers all standard metrics and provides
    typed update methods for the most common operations.
    """

    def __init__(self, registry: MetricsRegistry, node_id: str = "") -> None:
        self._registry = registry
        self.node_id = node_id

        # Task counters
        self.task_completed_total = registry.counter(
            "aeos_task_completed_total",
            "Total tasks completed",
            labels=("worker_id", "task_type"),
        )
        self.task_failed_total = registry.counter(
            "aeos_task_failed_total",
            "Total tasks failed",
            labels=("worker_id", "task_type", "reason"),
        )
        self.task_retried_total = registry.counter(
            "aeos_task_retried_total",
            "Total task retry attempts",
            labels=("worker_id", "task_type"),
        )
        self.task_cancelled_total = registry.counter(
            "aeos_task_cancelled_total",
            "Total tasks cancelled",
            labels=("worker_id",),
        )

        # Scheduling counters
        self.scheduling_decisions_total = registry.counter(
            "aeos_scheduling_decisions_total",
            "Total scheduling decisions",
            labels=("strategy", "outcome"),
        )

        # Worker gauges
        self.worker_in_flight = registry.gauge(
            "aeos_worker_in_flight_tasks",
            "Current in-flight task count per worker",
            labels=("worker_id",),
        )
        self.worker_queue_depth = registry.gauge(
            "aeos_worker_queue_depth",
            "Current task queue depth per worker",
            labels=("worker_id",),
        )
        self.worker_cpu_utilization = registry.gauge(
            "aeos_worker_cpu_utilization",
            "CPU utilization [0,1] per worker",
            labels=("worker_id",),
        )
        self.worker_memory_utilization = registry.gauge(
            "aeos_worker_memory_utilization",
            "Memory utilization [0,1] per worker",
            labels=("worker_id",),
        )
        self.cluster_healthy_workers = registry.gauge(
            "aeos_cluster_healthy_workers",
            "Number of healthy workers in the pool",
        )

        # Lease / fencing metrics
        self.lease_acquisitions_total = registry.counter(
            "aeos_lease_acquisitions_total",
            "Lease acquisition attempts",
            labels=("outcome",),
        )
        self.stale_token_rejections_total = registry.counter(
            "aeos_stale_token_rejections_total",
            "Stale fencing token rejections",
        )

        # Checkpoint metrics
        self.checkpoint_writes_total = registry.counter(
            "aeos_checkpoint_writes_total",
            "Checkpoint Phase 1 write count",
            labels=("type",),
        )
        self.checkpoint_commits_total = registry.counter(
            "aeos_checkpoint_commits_total",
            "Checkpoint Phase 2 commit count",
        )

        # Histograms
        self.task_duration_seconds = registry.histogram(
            "aeos_task_duration_seconds",
            "Task execution duration",
            labels=("task_type",),
        )
        self.scheduling_latency_seconds = registry.histogram(
            "aeos_scheduling_latency_seconds",
            "Scheduling decision latency",
        )
        self.checkpoint_latency_seconds = registry.histogram(
            "aeos_checkpoint_latency_seconds",
            "Checkpoint write+commit latency",
        )

        # Recovery metrics
        self.recovery_attempts_total = registry.counter(
            "aeos_recovery_attempts_total",
            "Recovery attempts by pattern",
            labels=("pattern",),
        )

    # ── Update helpers ────────────────────────────────────────────────────────

    def on_task_completed(self, worker_id: str, task_type: str, start_ns: int) -> None:
        self.task_completed_total.inc(worker_id=worker_id, task_type=task_type)
        self.task_duration_seconds.observe_seconds(start_ns)

    def on_task_failed(self, worker_id: str, task_type: str, reason: str) -> None:
        self.task_failed_total.inc(worker_id=worker_id, task_type=task_type, reason=reason)

    def update_worker_metrics(
        self,
        worker_id: str,
        *,
        in_flight: int = 0,
        queue_depth: int = 0,
        cpu: float = 0.0,
        memory: float = 0.0,
    ) -> None:
        self.worker_in_flight.set(in_flight, worker_id=worker_id)
        self.worker_queue_depth.set(queue_depth, worker_id=worker_id)
        self.worker_cpu_utilization.set(cpu, worker_id=worker_id)
        self.worker_memory_utilization.set(memory, worker_id=worker_id)
