"""
ML Platform — Production Monitoring
=====================================
Tracks model health in production: latency, throughput, confidence,
accuracy degradation, drift, and failure rate.

Architecture:
  MonitoringStore   → append-only event store (filesystem / future: TimescaleDB)
  ModelMonitor      → computes metrics from stored events
  AlertManager      → evaluates thresholds, emits alerts
  DriftChecker      → scheduled drift detection job (calls feature_store.drift)
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ml_platform.inference.schemas import InferenceResult, InferenceStatus


# ── Event schemas ──────────────────────────────────────────────────────────────

@dataclass
class InferenceEvent:
    """Emitted after every inference call. Written to the monitoring store."""
    event_id:       str
    model_id:       str
    request_id:     str
    status:         str                  # InferenceStatus.value
    latency_ms:     float
    confidence:     float | None
    timestamp:      str                  = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error:          str                  = ""
    metadata:       dict[str, Any]       = field(default_factory=dict)


@dataclass
class ModelHealthSnapshot:
    """Point-in-time health summary for a model."""
    model_id:           str
    snapshot_at:        str
    window_minutes:     int
    total_requests:     int
    success_count:      int
    failure_count:      int
    timeout_count:      int
    avg_latency_ms:     float
    p50_latency_ms:     float
    p95_latency_ms:     float
    p99_latency_ms:     float
    throughput_rpm:     float             # requests per minute
    failure_rate:       float             # 0.0 – 1.0
    avg_confidence:     float | None      = None
    drift_status:       str               = "unknown"
    health_status:      str               = "unknown"


# ── Monitoring store ───────────────────────────────────────────────────────────

class MonitoringStore:
    """
    Append-only JSONL event store.
    One file per model per day (rotated daily).

    Layout:
        <store_root>/
            <model_id>/
                2026-06-28.jsonl
                2026-06-29.jsonl
    """

    def __init__(self, store_root: str = "data/monitoring") -> None:
        self._root = Path(store_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def record_inference(self, result: InferenceResult) -> None:
        event = InferenceEvent(
            event_id=str(uuid.uuid4())[:8],
            model_id=result.model_id,
            request_id=result.request_id,
            status=result.status.value if hasattr(result.status, "value") else str(result.status),
            latency_ms=result.latency_ms,
            confidence=result.confidence,
            error=result.error,
        )
        self._append(event)

    def record_shadow_inference(self, result: InferenceResult) -> None:
        event = InferenceEvent(
            event_id=str(uuid.uuid4())[:8],
            model_id=result.model_id,
            request_id=result.request_id,
            status=result.status.value if hasattr(result.status, "value") else str(result.status),
            latency_ms=result.latency_ms,
            confidence=result.confidence,
            error=result.error,
            metadata={"shadow": True},
        )
        self._append(event)

    def read_events(
        self,
        model_id: str,
        date_str: str | None = None,
        last_n_minutes: int | None = None,
    ) -> list[InferenceEvent]:
        date_str = date_str or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_file = self._root / model_id / f"{date_str}.jsonl"
        if not log_file.exists():
            return []
        events = []
        with open(log_file) as f:
            for line in f:
                try:
                    events.append(InferenceEvent(**json.loads(line)))
                except Exception:
                    pass

        if last_n_minutes:
            cutoff = time.time() - last_n_minutes * 60
            events = [e for e in events if self._ts_to_epoch(e.timestamp) >= cutoff]

        return events

    def _append(self, event: InferenceEvent) -> None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log_dir = self._root / event.model_id
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / f"{date_str}.jsonl", "a") as f:
            f.write(json.dumps(asdict(event)) + "\n")

    @staticmethod
    def _ts_to_epoch(ts: str) -> float:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except Exception:
            return 0.0


# ── Model monitor ──────────────────────────────────────────────────────────────

class ModelMonitor:
    """
    Computes health snapshots from raw inference events.

    Usage:
        monitor = ModelMonitor(store)
        snapshot = monitor.snapshot(model_id="abc123", window_minutes=60)
        if snapshot.failure_rate > 0.05:
            alert_manager.trigger(...)
    """

    def __init__(self, store: MonitoringStore) -> None:
        self._store = store

    def snapshot(self, model_id: str, window_minutes: int = 60) -> ModelHealthSnapshot:
        events = self._store.read_events(model_id, last_n_minutes=window_minutes)
        return self._compute_snapshot(model_id, events, window_minutes)

    def _compute_snapshot(
        self,
        model_id: str,
        events: list[InferenceEvent],
        window_minutes: int,
    ) -> ModelHealthSnapshot:
        import statistics

        total = len(events)
        if total == 0:
            return ModelHealthSnapshot(
                model_id=model_id,
                snapshot_at=datetime.now(timezone.utc).isoformat(),
                window_minutes=window_minutes,
                total_requests=0,
                success_count=0,
                failure_count=0,
                timeout_count=0,
                avg_latency_ms=0.0,
                p50_latency_ms=0.0,
                p95_latency_ms=0.0,
                p99_latency_ms=0.0,
                throughput_rpm=0.0,
                failure_rate=0.0,
                health_status="no_data",
            )

        success   = sum(1 for e in events if e.status == InferenceStatus.SUCCESS.value)
        failures  = sum(1 for e in events if e.status == InferenceStatus.FAILED.value)
        timeouts  = sum(1 for e in events if e.status == InferenceStatus.TIMEOUT.value)
        latencies = sorted(e.latency_ms for e in events)
        confidences = [e.confidence for e in events if e.confidence is not None]

        def percentile(data: list[float], p: float) -> float:
            if not data:
                return 0.0
            idx = int(len(data) * p / 100)
            return data[min(idx, len(data) - 1)]

        failure_rate = (failures + timeouts) / total
        health = (
            "healthy" if failure_rate < 0.01 else
            "degraded" if failure_rate < 0.05 else
            "unhealthy"
        )

        return ModelHealthSnapshot(
            model_id=model_id,
            snapshot_at=datetime.now(timezone.utc).isoformat(),
            window_minutes=window_minutes,
            total_requests=total,
            success_count=success,
            failure_count=failures,
            timeout_count=timeouts,
            avg_latency_ms=round(statistics.mean(latencies), 2),
            p50_latency_ms=round(percentile(latencies, 50), 2),
            p95_latency_ms=round(percentile(latencies, 95), 2),
            p99_latency_ms=round(percentile(latencies, 99), 2),
            throughput_rpm=round(total / window_minutes, 2),
            failure_rate=round(failure_rate, 4),
            avg_confidence=round(statistics.mean(confidences), 4) if confidences else None,
            health_status=health,
        )
