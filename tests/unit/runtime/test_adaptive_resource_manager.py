"""Unit tests — AdaptiveResourceManager."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import CapabilityProfile
from app.runtime.adaptive_resource_manager import (
    AdaptiveResourceManager,
    ResourceAction,
    ResourceSnapshot,
)


def _snap(worker_id="w1", cpu=0.0, mem=0.0, queue=0, latency_ms=100.0) -> ResourceSnapshot:
    return ResourceSnapshot(
        worker_id=worker_id,
        cpu_utilization=cpu,
        memory_utilization=mem,
        queue_depth=queue,
        avg_latency_ms=latency_ms,
    )


class TestAdaptiveResourceManager:

    def test_no_action_for_normal_load(self):
        arm = AdaptiveResourceManager()
        decisions = arm.evaluate([_snap(cpu=0.5, mem=0.4)], [])
        assert all(d.action == ResourceAction.NO_ACTION for d in decisions)

    def test_scale_up_on_high_load(self):
        arm = AdaptiveResourceManager(load_high_threshold=0.8)
        decisions = arm.evaluate([_snap(cpu=0.9)], [])
        actions = [d.action for d in decisions]
        assert ResourceAction.SCALE_UP in actions

    def test_scale_down_on_low_load(self):
        arm = AdaptiveResourceManager(load_low_threshold=0.2)
        decisions = arm.evaluate([_snap(cpu=0.05, mem=0.05, queue=0)], [])
        actions = [d.action for d in decisions]
        assert ResourceAction.SCALE_DOWN in actions

    def test_rebalance_on_queue_overflow(self):
        arm = AdaptiveResourceManager(queue_overflow=32)
        decisions = arm.evaluate([_snap(queue=50)], [])
        actions = [d.action for d in decisions]
        assert ResourceAction.REBALANCE_QUEUE in actions

    def test_migrate_on_latency_spike(self):
        arm = AdaptiveResourceManager(latency_spike_factor=2.0)
        # First evaluation establishes baseline
        arm.evaluate([_snap(worker_id="w1", latency_ms=100.0)], [])
        # Second evaluation shows a spike
        decisions = arm.evaluate([_snap(worker_id="w1", latency_ms=350.0)], [])
        actions = [d.action for d in decisions]
        assert ResourceAction.MIGRATE_TASK in actions

    def test_urgency_in_range(self):
        arm = AdaptiveResourceManager(load_high_threshold=0.8)
        decisions = arm.evaluate([_snap(cpu=0.95)], [])
        for d in decisions:
            assert 0.0 <= d.urgency <= 1.0

    def test_multiple_workers_evaluated_independently(self):
        arm = AdaptiveResourceManager()
        snaps = [
            _snap("w1", cpu=0.9),   # high load → scale_up
            _snap("w2", cpu=0.1),   # low load → scale_down
        ]
        decisions = arm.evaluate(snaps, [])
        action_map = {d.target_worker_id: d.action for d in decisions}
        assert action_map.get("w1") == ResourceAction.SCALE_UP
        assert action_map.get("w2") == ResourceAction.SCALE_DOWN
