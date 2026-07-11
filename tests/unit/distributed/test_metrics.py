"""
Unit tests — MetricsRegistry, Counter, Gauge, Histogram, RuntimeMetricsCollector.
"""

from __future__ import annotations

import time
import pytest

from app.distributed.metrics.registry import Counter, Gauge, Histogram, MetricsRegistry
from app.distributed.metrics.collectors import RuntimeMetricsCollector


class TestCounter:

    def test_starts_at_zero(self):
        c = Counter("test_total")
        assert c.get() == 0.0

    def test_increment(self):
        c = Counter("test_total")
        c.inc()
        c.inc(3)
        assert c.get() == 4.0

    def test_labels(self):
        c = Counter("test_total", labels=("task_type",))
        c.inc(task_type="echo")
        c.inc(2.0, task_type="echo")
        c.inc(task_type="other")
        assert c.get(task_type="echo") == 3.0
        assert c.get(task_type="other") == 1.0
        assert c.get(task_type="missing") == 0.0

    def test_collect(self):
        c = Counter("test_total", labels=("priority",))
        c.inc(priority="high")
        c.inc(priority="normal")
        data = c.collect()
        assert len(data) == 2


class TestGauge:

    def test_set_and_get(self):
        g = Gauge("test_gauge")
        g.set(42.0)
        assert g.get() == 42.0

    def test_inc_dec(self):
        g = Gauge("test_gauge")
        g.inc(5)
        g.dec(2)
        assert g.get() == 3.0

    def test_negative_value(self):
        g = Gauge("test_gauge")
        g.set(-10.0)
        assert g.get() == -10.0

    def test_labels(self):
        g = Gauge("worker_in_flight", labels=("worker_id",))
        g.set(4, worker_id="w1")
        g.set(7, worker_id="w2")
        assert g.get(worker_id="w1") == 4
        assert g.get(worker_id="w2") == 7


class TestHistogram:

    def test_observe_and_collect(self):
        h = Histogram("latency_seconds")
        h.observe(0.1)
        h.observe(0.5)
        h.observe(2.0)
        data = h.collect()
        entry = list(data.values())[0]
        assert entry["count"] == 3
        assert abs(entry["sum"] - 2.6) < 0.001

    def test_observe_seconds(self):
        h = Histogram("duration_seconds")
        start = time.monotonic_ns()
        h.observe_seconds(start)
        data = h.collect()
        entry = list(data.values())[0]
        assert entry["count"] == 1
        assert 0 <= entry["sum"] < 1.0   # should be very fast

    def test_bucket_counts(self):
        h = Histogram("latency", buckets=(0.1, 0.5, 1.0))
        h.observe(0.05)   # ≤ 0.1
        h.observe(0.3)    # ≤ 0.5
        h.observe(0.8)    # ≤ 1.0
        h.observe(2.0)    # > all buckets
        data = h.collect()
        entry = list(data.values())[0]
        # Bucket counts: 0.1 → 1, 0.5 → 2, 1.0 → 3 (cumulative)
        assert entry["buckets"][0] == 1   # ≤ 0.1
        assert entry["buckets"][1] == 2   # ≤ 0.5
        assert entry["buckets"][2] == 3   # ≤ 1.0


class TestMetricsRegistry:

    def test_register_and_retrieve_counter(self):
        reg = MetricsRegistry("node-1")
        c = reg.counter("test_total", "A test counter", labels=("label",))
        c.inc(label="x")
        same = reg.counter("test_total", labels=("label",))
        assert same.get(label="x") == 1.0

    def test_idempotent_registration(self):
        reg = MetricsRegistry()
        c1 = reg.counter("my_counter")
        c2 = reg.counter("my_counter")
        assert c1 is c2

    def test_collect_all_structure(self):
        reg = MetricsRegistry()
        reg.counter("c1").inc()
        reg.gauge("g1").set(5)
        reg.histogram("h1").observe(0.1)
        all_data = reg.collect_all()
        assert "counters" in all_data
        assert "gauges" in all_data
        assert "histograms" in all_data
        assert "c1" in all_data["counters"]
        assert "g1" in all_data["gauges"]
        assert "h1" in all_data["histograms"]


class TestRuntimeMetricsCollector:

    def test_on_task_completed_increments_counter(self):
        reg = MetricsRegistry()
        coll = RuntimeMetricsCollector(reg, node_id="w1")
        start_ns = time.monotonic_ns()
        coll.on_task_completed("worker-1", "echo", start_ns)
        assert coll.task_completed_total.get(worker_id="worker-1", task_type="echo") == 1.0

    def test_on_task_failed_increments_counter(self):
        reg = MetricsRegistry()
        coll = RuntimeMetricsCollector(reg)
        coll.on_task_failed("worker-1", "echo", "timeout")
        assert coll.task_failed_total.get(worker_id="worker-1", task_type="echo", reason="timeout") == 1.0

    def test_update_worker_metrics(self):
        reg = MetricsRegistry()
        coll = RuntimeMetricsCollector(reg, node_id="w1")
        coll.update_worker_metrics("w1", in_flight=3, queue_depth=5, cpu=0.7, memory=0.4)
        assert coll.worker_in_flight.get(worker_id="w1") == 3.0
        assert coll.worker_cpu_utilization.get(worker_id="w1") == 0.7
