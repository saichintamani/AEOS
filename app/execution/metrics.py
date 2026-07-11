"""
AEOS Distributed Execution Engine — Metrics Collector

Automatic per-node and per-workflow metrics. Zero external dependencies.
Collects: latency (p50/p95/p99), success/failure rates, retry counts,
token costs, confidence scores, and parallel efficiency.

Future: swap InMemoryMetricsStore for Prometheus/OTLP via adapters.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.core.logger import get_logger
from app.execution.schemas import StepResult, StepStatus

__all__ = [
    "NodeMetrics",
    "WorkflowMetrics",
    "MetricsCollector",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Compute a percentile from a sorted list. Returns 0.0 if empty."""
    if not sorted_values:
        return 0.0
    idx = max(0, math.ceil(pct / 100 * len(sorted_values)) - 1)
    return sorted_values[idx]


@dataclass
class NodeMetrics:
    """Cumulative metrics for a single node_id across all executions."""
    node_id: str
    execution_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    timeout_count: int = 0
    retry_count: int = 0
    total_tokens: int = 0
    total_latency_ms: float = 0.0
    _latencies: list[float] = field(default_factory=list, repr=False)
    _confidence_sum: float = 0.0
    last_executed_at: str = ""
    last_status: str = ""

    # ── Computed properties ───────────────────────────────────────────────────

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.execution_count, 1)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / max(self.execution_count, 1)

    @property
    def p50_ms(self) -> float:
        return _percentile(self._latencies, 50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self._latencies, 95)

    @property
    def p99_ms(self) -> float:
        return _percentile(self._latencies, 99)

    @property
    def avg_confidence(self) -> float:
        return self._confidence_sum / max(self.execution_count, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "execution_count": self.execution_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "timeout_count": self.timeout_count,
            "retry_count": self.retry_count,
            "success_rate": round(self.success_rate, 4),
            "total_tokens": self.total_tokens,
            "latency": {
                "avg_ms": round(self.avg_latency_ms, 1),
                "p50_ms": round(self.p50_ms, 1),
                "p95_ms": round(self.p95_ms, 1),
                "p99_ms": round(self.p99_ms, 1),
            },
            "avg_confidence": round(self.avg_confidence, 3),
            "last_executed_at": self.last_executed_at,
            "last_status": self.last_status,
        }


@dataclass
class WorkflowMetrics:
    """Aggregate metrics for a single workflow execution."""
    workflow_id: str
    trace_id: str = ""
    total_nodes: int = 0
    completed_nodes: int = 0
    failed_nodes: int = 0
    skipped_nodes: int = 0
    total_latency_ms: float = 0.0
    wall_time_ms: float = 0.0         # actual elapsed from start to end
    parallel_efficiency: float = 0.0   # wall_time / sum(node_latencies); < 1.0 = good parallelism
    total_tokens: int = 0
    avg_quality_score: float = 0.0
    node_latencies: dict[str, float] = field(default_factory=dict)
    started_at: str = field(default_factory=_now)
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "trace_id": self.trace_id,
            "nodes": {
                "total": self.total_nodes,
                "completed": self.completed_nodes,
                "failed": self.failed_nodes,
                "skipped": self.skipped_nodes,
            },
            "latency": {
                "total_node_ms": round(self.total_latency_ms, 1),
                "wall_time_ms": round(self.wall_time_ms, 1),
                "parallel_efficiency": round(self.parallel_efficiency, 3),
            },
            "total_tokens": self.total_tokens,
            "avg_quality_score": round(self.avg_quality_score, 3),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class MetricsCollector:
    """
    Thread-safe (single-process) metrics collector for the execution engine.

    Usage:
        collector = MetricsCollector()
        collector.record_step(workflow_id, step_result)
        print(collector.node_metrics("my_node_id").p95_ms)
    """

    def __init__(self) -> None:
        # node_id → NodeMetrics
        self._node_metrics: dict[str, NodeMetrics] = {}
        # workflow_id → WorkflowMetrics
        self._workflow_metrics: dict[str, WorkflowMetrics] = {}
        # workflow_id → [step_results] for latency ordering
        self._workflow_steps: dict[str, list[StepResult]] = defaultdict(list)

    # ── Recording ─────────────────────────────────────────────────────────────

    def record_step(self, workflow_id: str, step: StepResult) -> None:
        """Record a completed step result into both node and workflow metrics."""
        self._record_node(step)
        self._record_workflow_step(workflow_id, step)

    def record_retry(self, workflow_id: str, node_id: str) -> None:
        """Increment retry counter for a node."""
        nm = self._node_metrics.setdefault(node_id, NodeMetrics(node_id=node_id))
        nm.retry_count += 1

    def start_workflow(
        self,
        workflow_id: str,
        trace_id: str = "",
        total_nodes: int = 0,
    ) -> None:
        """Initialize workflow metrics at the start of execution."""
        self._workflow_metrics[workflow_id] = WorkflowMetrics(
            workflow_id=workflow_id,
            trace_id=trace_id,
            total_nodes=total_nodes,
        )

    def finish_workflow(
        self,
        workflow_id: str,
        wall_time_ms: float = 0.0,
        avg_quality_score: float = 0.0,
    ) -> None:
        """Finalize workflow metrics at the end of execution."""
        wm = self._workflow_metrics.get(workflow_id)
        if wm is None:
            return
        wm.wall_time_ms = wall_time_ms
        wm.completed_at = _now()
        wm.avg_quality_score = avg_quality_score
        # Parallel efficiency: sum of node latencies vs wall time
        if wall_time_ms > 0 and wm.total_latency_ms > 0:
            wm.parallel_efficiency = round(wall_time_ms / wm.total_latency_ms, 3)

    # ── Query ─────────────────────────────────────────────────────────────────

    def node_metrics(self, node_id: str) -> NodeMetrics | None:
        return self._node_metrics.get(node_id)

    def workflow_metrics(self, workflow_id: str) -> WorkflowMetrics | None:
        return self._workflow_metrics.get(workflow_id)

    def all_node_metrics(self) -> list[NodeMetrics]:
        return list(self._node_metrics.values())

    def summary(self) -> dict[str, Any]:
        total_executions = sum(nm.execution_count for nm in self._node_metrics.values())
        total_failures = sum(nm.failure_count for nm in self._node_metrics.values())
        return {
            "tracked_nodes": len(self._node_metrics),
            "tracked_workflows": len(self._workflow_metrics),
            "total_node_executions": total_executions,
            "total_node_failures": total_failures,
            "overall_success_rate": round(
                1.0 - total_failures / max(total_executions, 1), 4
            ),
        }

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        lines: list[str] = [
            "# HELP aeos_node_executions_total Total node executions",
            "# TYPE aeos_node_executions_total counter",
        ]
        for nm in self._node_metrics.values():
            nid = nm.node_id.replace("-", "_")
            lines.append(f'aeos_node_executions_total{{node_id="{nm.node_id}"}} {nm.execution_count}')
        lines += [
            "",
            "# HELP aeos_node_latency_p95_ms P95 node latency in ms",
            "# TYPE aeos_node_latency_p95_ms gauge",
        ]
        for nm in self._node_metrics.values():
            lines.append(f'aeos_node_latency_p95_ms{{node_id="{nm.node_id}"}} {nm.p95_ms:.1f}')
        lines += [
            "",
            "# HELP aeos_node_success_rate Node success rate (0-1)",
            "# TYPE aeos_node_success_rate gauge",
        ]
        for nm in self._node_metrics.values():
            lines.append(f'aeos_node_success_rate{{node_id="{nm.node_id}"}} {nm.success_rate:.4f}')
        return "\n".join(lines) + "\n"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _record_node(self, step: StepResult) -> None:
        nm = self._node_metrics.setdefault(
            step.node_id, NodeMetrics(node_id=step.node_id)
        )
        nm.execution_count += 1
        nm.total_tokens += step.token_cost
        nm.last_executed_at = step.produced_at
        nm.last_status = step.status.value

        if step.status == StepStatus.COMPLETED:
            nm.success_count += 1
        elif step.status == StepStatus.TIMED_OUT:
            nm.timeout_count += 1
        else:
            nm.failure_count += 1

        if step.latency_ms > 0:
            # Insert sorted for percentile calculation
            bisect.insort(nm._latencies, step.latency_ms)
            nm.total_latency_ms += step.latency_ms

        nm._confidence_sum += step.confidence

    def _record_workflow_step(self, workflow_id: str, step: StepResult) -> None:
        wm = self._workflow_metrics.get(workflow_id)
        if wm is None:
            return
        self._workflow_steps[workflow_id].append(step)
        wm.total_latency_ms += step.latency_ms
        wm.total_tokens += step.token_cost
        wm.node_latencies[step.node_id] = step.latency_ms

        if step.status == StepStatus.COMPLETED:
            wm.completed_nodes += 1
        elif step.status == StepStatus.SKIPPED:
            wm.skipped_nodes += 1
        else:
            wm.failed_nodes += 1
