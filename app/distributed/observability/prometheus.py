"""
Phase 9B.6 Priority 3 — Prometheus Text Format Exporter

Converts AEOS MetricsRegistry into Prometheus text format 0.0.4.
Works alongside the internal MetricsRegistry without requiring the
`prometheus_client` package (zero new dependencies).

Usage::

    from app.distributed.observability.prometheus import PrometheusExporter
    from app.distributed.metrics.registry import MetricsRegistry

    registry = MetricsRegistry(node_id="api-0")
    exporter = PrometheusExporter(registry)
    text = exporter.export()          # Prometheus text format
    # Serve at GET /metrics
"""

from __future__ import annotations

import time
from typing import Any

from app.distributed.metrics.registry import MetricsRegistry


class PrometheusExporter:
    """
    Renders a MetricsRegistry snapshot as Prometheus text format 0.0.4.

    Label pairs in label keys are stored as sorted tuples of (k,v) pairs
    by MetricsRegistry — this class reconstructs the label string.
    """

    # HELP strings for each metric name (kept here to avoid coupling the
    # registry to presentation concerns).
    _HELP: dict[str, str] = {
        "aeos_task_completed_total":       "Total tasks completed successfully",
        "aeos_task_failed_total":          "Total tasks that failed",
        "aeos_task_retried_total":         "Total task retry attempts",
        "aeos_task_cancelled_total":       "Total tasks cancelled",
        "aeos_scheduling_decisions_total": "Total scheduling decisions made",
        "aeos_worker_in_flight_tasks":     "Current in-flight task count per worker",
        "aeos_worker_queue_depth":         "Current task queue depth per worker",
        "aeos_worker_cpu_utilization":     "CPU utilization [0,1] per worker",
        "aeos_worker_memory_utilization":  "Memory utilization [0,1] per worker",
        "aeos_cluster_healthy_workers":    "Number of healthy workers in the cluster",
        "aeos_lease_acquisitions_total":   "Lease acquisition attempts by outcome",
        "aeos_stale_token_rejections_total": "Stale fencing token rejections",
        "aeos_checkpoint_writes_total":    "Checkpoint Phase-1 write count",
        "aeos_checkpoint_commits_total":   "Checkpoint Phase-2 commit count",
        "aeos_task_duration_seconds":      "Task execution duration in seconds",
        "aeos_scheduling_latency_seconds": "Scheduling decision latency in seconds",
        "aeos_checkpoint_latency_seconds": "Checkpoint write+commit latency in seconds",
        "aeos_recovery_attempts_total":    "Recovery attempts by pattern",
        # Invariant engine metrics
        "aeos_invariant_evaluations_total":  "Total invariant evaluation cycles",
        "aeos_invariant_violations_total":   "Total invariant violations detected",
        "aeos_invariant_checks_registered":  "Number of registered invariant checks",
        # API metrics
        "aeos_http_requests_total":          "Total HTTP requests by method, path, status",
        "aeos_http_request_duration_seconds": "HTTP request duration in seconds",
    }

    def __init__(self, registry: MetricsRegistry, node_id: str = "") -> None:
        self._registry = registry
        self._node_id = node_id or registry.node_id

    def export(self) -> str:
        """Return Prometheus text format 0.0.4 string."""
        snapshot = self._registry.collect_all()
        lines: list[str] = [
            f"# AEOS metrics — node_id={self._node_id} timestamp={time.time():.3f}",
        ]

        # Counters
        for name, series in snapshot.get("counters", {}).items():
            lines.append(f"# HELP {name} {self._HELP.get(name, name)}")
            lines.append(f"# TYPE {name} counter")
            for label_key, value in series.items():
                label_str = self._format_labels(label_key)
                lines.append(f"{name}{label_str} {value:.6g}")

        # Gauges
        for name, series in snapshot.get("gauges", {}).items():
            lines.append(f"# HELP {name} {self._HELP.get(name, name)}")
            lines.append(f"# TYPE {name} gauge")
            for label_key, value in series.items():
                label_str = self._format_labels(label_key)
                lines.append(f"{name}{label_str} {value:.6g}")

        # Histograms
        for name, data in snapshot.get("histograms", {}).items():
            lines.append(f"# HELP {name} {self._HELP.get(name, name)}")
            lines.append(f"# TYPE {name} histogram")
            if isinstance(data, dict) and name in data:
                hdata: dict[str, Any] = data[name]
                bounds: list[float] = hdata.get("bucket_bounds", [])
                counts: list[int]   = hdata.get("buckets", [])
                cumulative = 0
                for upper, cnt in zip(bounds, counts):
                    cumulative += cnt
                    lines.append(f'{name}_bucket{{le="{upper}"}} {cumulative}')
                lines.append(f'{name}_bucket{{le="+Inf"}} {hdata.get("count", 0)}')
                lines.append(f"{name}_sum {hdata.get('sum', 0.0):.6g}")
                lines.append(f"{name}_count {hdata.get('count', 0)}")

        lines.append("")  # trailing newline
        return "\n".join(lines)

    @staticmethod
    def _format_labels(label_key: tuple | Any) -> str:
        """Convert a MetricsRegistry label key (sorted tuple of pairs) to {k="v",...}."""
        if not label_key:
            return ""
        if isinstance(label_key, tuple) and label_key:
            pairs = [f'{k}="{v}"' for k, v in label_key]
            return "{" + ",".join(pairs) + "}"
        return ""


# ── Global API-level metrics (request counters) ───────────────────────────────

class APIMetrics:
    """
    Lightweight request metrics for FastAPI middleware.
    Integrates with the global MetricsRegistry to expose HTTP metrics.
    """

    def __init__(self, registry: MetricsRegistry) -> None:
        self.requests_total = registry.counter(
            "aeos_http_requests_total",
            "Total HTTP requests",
            labels=("method", "path", "status_code"),
        )
        self.request_duration = registry.histogram(
            "aeos_http_request_duration_seconds",
            "HTTP request duration",
            labels=("method", "path"),
        )
        self._start: dict[str, int] = {}

    def record_request(
        self,
        method: str,
        path: str,
        status_code: int,
        start_ns: int,
    ) -> None:
        self.requests_total.inc(
            method=method,
            path=self._normalize_path(path),
            status_code=str(status_code),
        )
        elapsed = (time.monotonic_ns() - start_ns) / 1e9
        self.request_duration.observe(elapsed)

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Collapse UUIDs and IDs in paths to avoid high cardinality."""
        import re
        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f-]{27}", "/{id}", path)
        path = re.sub(r"/\d+", "/{id}", path)
        return path
