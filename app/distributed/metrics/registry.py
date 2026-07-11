"""
Prometheus-compatible in-process metrics.

Counter  — monotonically increasing float counter with optional label sets.
Gauge    — settable float value with optional label sets.
Histogram — observes durations; buckets are configurable.
MetricsRegistry — idempotent registration, collect_all() export.

Thread-safe via threading.Lock (metrics may be updated from multiple asyncio tasks
that run on different threads in tests using ThreadPoolExecutor).

Contract: AC-OBS-001
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any

_DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


def _label_key(kwargs: dict[str, str]) -> tuple:
    return tuple(sorted(kwargs.items()))


class Counter:
    """Monotonically increasing counter with optional label dimensions."""

    def __init__(self, name: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self._labels = labels
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        key = _label_key(label_values)
        with self._lock:
            self._values[key] += amount

    def get(self, **label_values: str) -> float:
        key = _label_key(label_values)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> dict[tuple, float]:
        with self._lock:
            return dict(self._values)


class Gauge:
    """A settable float value with optional label dimensions."""

    def __init__(self, name: str, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self._labels = labels
        self._values: dict[tuple, float] = defaultdict(float)
        self._lock = threading.Lock()

    def set(self, value: float, **label_values: str) -> None:
        key = _label_key(label_values)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **label_values: str) -> None:
        key = _label_key(label_values)
        with self._lock:
            self._values[key] += amount

    def dec(self, amount: float = 1.0, **label_values: str) -> None:
        key = _label_key(label_values)
        with self._lock:
            self._values[key] -= amount

    def get(self, **label_values: str) -> float:
        key = _label_key(label_values)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> dict[tuple, float]:
        with self._lock:
            return dict(self._values)


class Histogram:
    """Fixed-bucket histogram for duration/size observations."""

    def __init__(
        self,
        name: str,
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self._labels = labels
        self._buckets = sorted(buckets)
        self._lock = threading.Lock()
        self._count = 0
        self._sum = 0.0
        self._bucket_counts = [0] * len(self._buckets)

    def observe(self, value: float) -> None:
        with self._lock:
            self._count += 1
            self._sum += value
            for i, upper in enumerate(self._buckets):
                if value <= upper:
                    self._bucket_counts[i] += 1

    def observe_seconds(self, start_ns: int) -> None:
        elapsed = (time.monotonic_ns() - start_ns) / 1e9
        self.observe(elapsed)

    def collect(self) -> dict[str, Any]:
        with self._lock:
            return {
                self.name: {
                    "count": self._count,
                    "sum": self._sum,
                    "buckets": list(self._bucket_counts),
                    "bucket_bounds": list(self._buckets),
                }
            }


class MetricsRegistry:
    """
    Registry for all metrics in a node.

    Idempotent: registering the same name twice returns the same object.
    """

    def __init__(self, node_id: str = "") -> None:
        self.node_id = node_id
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(
        self,
        name: str,
        description: str = "",
        labels: tuple[str, ...] = (),
    ) -> Counter:
        with self._lock:
            if name not in self._counters:
                self._counters[name] = Counter(name, labels)
            return self._counters[name]

    def gauge(
        self,
        name: str,
        description: str = "",
        labels: tuple[str, ...] = (),
    ) -> Gauge:
        with self._lock:
            if name not in self._gauges:
                self._gauges[name] = Gauge(name, labels)
            return self._gauges[name]

    def histogram(
        self,
        name: str,
        description: str = "",
        labels: tuple[str, ...] = (),
        buckets: tuple[float, ...] = _DEFAULT_BUCKETS,
    ) -> Histogram:
        with self._lock:
            if name not in self._histograms:
                self._histograms[name] = Histogram(name, labels, buckets)
            return self._histograms[name]

    def collect_all(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters":   {name: c.collect() for name, c in self._counters.items()},
                "gauges":     {name: g.collect() for name, g in self._gauges.items()},
                "histograms": {name: h.collect() for name, h in self._histograms.items()},
                "node_id":    self.node_id,
            }
