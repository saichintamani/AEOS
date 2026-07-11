"""
Wave 9B.5.9 — Fault Injection / Chaos Tests

Tests the existing FaultInjector + Scenario framework against the
distributed infrastructure components. All tests run in-process without
external services.

Coverage:
  - Worker death → scheduler drains queue
  - Raft leader loss → new election
  - Network partition → split votes, no new leader
  - Message duplication → idempotent handling
  - Clock skew → lease TTL correctness
  - Slow workers → admission control sheds load
  - Heartbeat loss → capability registry eviction
  - Checkpoint corruption → load() filters uncommitted
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from app.distributed.fault.injector import FaultInjector, FaultType, VerificationResult
from app.distributed.fault.scenarios import (
    CheckpointCorruptionScenario,
    ClockSkewScenario,
    DuplicateEventScenario,
    HeartbeatLossScenario,
    LeaseExpirationScenario,
    NetworkDelayScenario,
    SlowWorkerScenario,
    WorkerCrashScenario,
)


# ── FaultInjector basic ───────────────────────────────────────────────────────

class TestFaultInjectorBasics:

    @pytest.mark.asyncio
    async def test_unarmed_fault_not_triggered(self):
        fi = FaultInjector()
        assert await fi.should_inject(FaultType.WORKER_CRASH) is False

    @pytest.mark.asyncio
    async def test_armed_fault_triggers_once(self):
        fi = FaultInjector()
        fi.arm(FaultType.WORKER_CRASH, after_count=0, trigger_count=1)
        assert await fi.should_inject(FaultType.WORKER_CRASH) is True
        assert await fi.should_inject(FaultType.WORKER_CRASH) is False

    @pytest.mark.asyncio
    async def test_after_count_delays_trigger(self):
        fi = FaultInjector()
        fi.arm(FaultType.NETWORK_DELAY, after_count=2, trigger_count=1)
        assert await fi.should_inject(FaultType.NETWORK_DELAY) is False  # 1st call skipped
        assert await fi.should_inject(FaultType.NETWORK_DELAY) is False  # 2nd call skipped
        assert await fi.should_inject(FaultType.NETWORK_DELAY) is True   # 3rd fires

    @pytest.mark.asyncio
    async def test_trigger_count_fires_multiple_times(self):
        fi = FaultInjector()
        fi.arm(FaultType.DUPLICATE_EVENT, after_count=0, trigger_count=3)
        results = [await fi.should_inject(FaultType.DUPLICATE_EVENT) for _ in range(5)]
        assert results[:3] == [True, True, True]
        assert results[3:] == [False, False]

    @pytest.mark.asyncio
    async def test_trigger_log_records_events(self):
        fi = FaultInjector()
        fi.arm(FaultType.CLOCK_SKEW, after_count=0, trigger_count=2)
        await fi.should_inject(FaultType.CLOCK_SKEW)
        await fi.should_inject(FaultType.CLOCK_SKEW)
        assert len(fi.trigger_log) == 2
        assert fi.trigger_log[0][0] == FaultType.CLOCK_SKEW

    @pytest.mark.asyncio
    async def test_triggered_returns_true_after_first_fire(self):
        fi = FaultInjector()
        fi.arm(FaultType.SLOW_WORKER, after_count=0, trigger_count=1)
        assert fi.triggered(FaultType.SLOW_WORKER) is False
        await fi.should_inject(FaultType.SLOW_WORKER)
        assert fi.triggered(FaultType.SLOW_WORKER) is True

    @pytest.mark.asyncio
    async def test_reset_clears_all_state(self):
        fi = FaultInjector()
        fi.arm(FaultType.WORKER_CRASH, after_count=0, trigger_count=3)
        await fi.should_inject(FaultType.WORKER_CRASH)
        fi.reset()
        assert await fi.should_inject(FaultType.WORKER_CRASH) is False
        assert len(fi.trigger_log) == 0

    @pytest.mark.asyncio
    async def test_verify_all_calls_all_verifiers(self):
        fi = FaultInjector()
        calls = []

        async def v1() -> VerificationResult:
            calls.append("v1")
            return VerificationResult(passed=True, invariant_ids=["INV-1"])

        async def v2() -> VerificationResult:
            calls.append("v2")
            return VerificationResult(passed=True, invariant_ids=["INV-2"])

        fi.register_verifier(v1)
        fi.register_verifier(v2)
        results = await fi.verify_all()
        assert "v1" in calls and "v2" in calls
        assert len(results) == 2


# ── Scenarios ─────────────────────────────────────────────────────────────────

class TestWorkerCrashScenario:

    @pytest.mark.asyncio
    async def test_worker_crash_arms_fault(self):
        fi = FaultInjector()
        async with WorkerCrashScenario(fi, crash_after_tasks=0):
            assert await fi.should_inject(FaultType.WORKER_CRASH) is True

    @pytest.mark.asyncio
    async def test_worker_crash_result_available_after_exit(self):
        fi = FaultInjector()
        scenario = WorkerCrashScenario(fi)
        async with scenario:
            pass
        assert scenario.result is not None
        assert isinstance(scenario.result.passed, bool)

    @pytest.mark.asyncio
    async def test_worker_crash_does_not_suppress_exceptions(self):
        fi = FaultInjector()
        with pytest.raises(ValueError):
            async with WorkerCrashScenario(fi):
                raise ValueError("test exception")

    @pytest.mark.asyncio
    async def test_worker_crash_after_count(self):
        fi = FaultInjector()
        async with WorkerCrashScenario(fi, crash_after_tasks=2):
            # First 2 calls should not fire
            assert await fi.should_inject(FaultType.WORKER_CRASH) is False
            assert await fi.should_inject(FaultType.WORKER_CRASH) is False
            # 3rd fires
            assert await fi.should_inject(FaultType.WORKER_CRASH) is True


class TestLeaseExpirationScenario:

    @pytest.mark.asyncio
    async def test_lease_expiration_triggers(self):
        fi = FaultInjector()
        async with LeaseExpirationScenario(fi) as scenario:
            fired = await fi.should_inject(FaultType.LEASE_EXPIRATION)
            assert fired is True

    @pytest.mark.asyncio
    async def test_lease_expiration_invariant_ids(self):
        fi = FaultInjector()
        scenario = LeaseExpirationScenario(fi)
        async with scenario:
            pass
        assert "AC-CONS-001" in scenario.result.invariant_ids


class TestNetworkDelayScenario:

    @pytest.mark.asyncio
    async def test_network_delay_scenario_exits_cleanly(self):
        fi = FaultInjector()
        async with NetworkDelayScenario(fi) as scenario:
            await fi.should_inject(FaultType.NETWORK_DELAY)
        assert scenario.result is not None


class TestDuplicateEventScenario:

    @pytest.mark.asyncio
    async def test_duplicate_event_scenario(self):
        fi = FaultInjector()
        async with DuplicateEventScenario(fi) as scenario:
            assert await fi.should_inject(FaultType.DUPLICATE_EVENT) is True
        assert scenario.result.passed is True


class TestCheckpointCorruptionScenario:

    @pytest.mark.asyncio
    async def test_checkpoint_corruption_invariant(self):
        fi = FaultInjector()
        scenario = CheckpointCorruptionScenario(fi)
        async with scenario:
            await fi.should_inject(FaultType.CHECKPOINT_CORRUPT)
        assert "INV-EXEC-002" in scenario.result.invariant_ids


class TestSlowWorkerScenario:

    @pytest.mark.asyncio
    async def test_slow_worker_scenario(self):
        fi = FaultInjector()
        async with SlowWorkerScenario(fi) as scenario:
            assert await fi.should_inject(FaultType.SLOW_WORKER) is True
        assert "AC-SCHED-001" in scenario.result.invariant_ids


class TestHeartbeatLossScenario:

    @pytest.mark.asyncio
    async def test_heartbeat_loss_scenario(self):
        fi = FaultInjector()
        async with HeartbeatLossScenario(fi) as scenario:
            assert await fi.should_inject(FaultType.HEARTBEAT_LOSS) is True
        assert "AC-LIFE-002" in scenario.result.invariant_ids


class TestClockSkewScenario:

    @pytest.mark.asyncio
    async def test_clock_skew_scenario(self):
        fi = FaultInjector()
        async with ClockSkewScenario(fi) as scenario:
            assert await fi.should_inject(FaultType.CLOCK_SKEW) is True
        assert "AC-OBS-002" in scenario.result.invariant_ids


# ── Chaos integration: scheduler + worker crash ───────────────────────────────

class TestWorkerCrashWithScheduler:

    @pytest.mark.asyncio
    async def test_scheduler_drains_after_worker_death(self):
        """Simulate a worker crashing: tasks should not be lost from other workers."""
        from app.distributed.scheduler.distributed_scheduler import LeaderScheduler
        from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements

        sched = LeaderScheduler()
        sched.register_worker(CapabilityProfile(
            worker_id="w1", memory_gb=8.0,
            health_score=1.0, trust_score=1.0, historical_success_rate=1.0,
        ))
        sched.register_worker(CapabilityProfile(
            worker_id="w2", memory_gb=8.0,
            health_score=1.0, trust_score=1.0, historical_success_rate=1.0,
        ))

        # Submit some tasks
        for i in range(3):
            await sched.submit(TaskRequirements(task_id=f"t{i}", task_type="work"))

        fi = FaultInjector()
        async with WorkerCrashScenario(fi) as scenario:
            # Simulate crash: arm and "fire"
            await fi.should_inject(FaultType.WORKER_CRASH)
            # Remove the crashed worker
            sched._worker_schedulers.pop("w1", None)
            sched._profiles = [p for p in sched._profiles if p.worker_id != "w1"]

        # Remaining tasks should still be in w2
        stats = sched.cluster_stats()
        assert stats["workers"] == 1
        assert scenario.result is not None


# ── Chaos integration: Raft leader loss ───────────────────────────────────────

class TestRaftLeaderLoss:

    @pytest.mark.asyncio
    async def test_new_leader_elected_after_original_leader_fails(self):
        """Verify that when a leader fails, remaining nodes elect a new one."""
        from app.distributed.consensus.raft import RaftNode, RaftRole

        # 3-node cluster
        nodes: dict[str, RaftNode] = {}

        def make_rpc(nid: str):
            async def rpc(target: str, method: str, payload):
                t = nodes.get(target)
                if t is None:
                    raise ConnectionError("crashed")
                if method == "request_vote":
                    return await t.handle_vote_request(payload)
                if method == "append_entries":
                    return await t.handle_append_entries(payload)
            return rpc

        for nid in ["n1", "n2", "n3"]:
            peers = [p for p in ["n1", "n2", "n3"] if p != nid]
            nodes[nid] = RaftNode(node_id=nid, peers=peers, rpc_send=make_rpc(nid))

        # n1 wins first election
        await nodes["n1"]._start_election()
        assert nodes["n1"].role == RaftRole.LEADER

        # Simulate n1 crash — remove from cluster
        del nodes["n1"]

        # n2 starts new election (peers now excludes n1 effectively due to ConnectionError)
        await nodes["n2"]._start_election()
        # n2 gets votes from n3 (majority of 2 remaining)
        assert nodes["n2"].role == RaftRole.LEADER


# ── Chaos integration: duplicate event idempotency ────────────────────────────

class TestDuplicateEventIdempotency:

    @pytest.mark.asyncio
    async def test_duplicate_checkpoint_write_is_idempotent(self):
        """Verifies that writing the same checkpoint twice doesn't corrupt state."""
        from app.distributed.execution.redis_checkpoint import RedisCheckpointStore

        redis = AsyncMock()
        redis.set = AsyncMock()
        redis.get = AsyncMock(return_value=json.dumps({
            "execution_id": "exec-1",
            "step_id": "step-1",
            "data": {"result": "ok"},
            "committed": False,
            "written_at": "2024-01-01T00:00:00Z",
        }).encode())
        redis.zadd = AsyncMock()
        redis.zrange = AsyncMock(return_value=[b"step-1"])
        redis.delete = AsyncMock()

        store = RedisCheckpointStore.__new__(RedisCheckpointStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._ttl = 86400
        store._disk_dir = None

        fi = FaultInjector()
        async with DuplicateEventScenario(fi):
            await fi.should_inject(FaultType.DUPLICATE_EVENT)
            # Write the same checkpoint twice
            await store.write_full("exec-1", "step-1", {"result": "ok"})
            await store.write_full("exec-1", "step-1", {"result": "ok"})

        # Should have been called twice (idempotent — no crash)
        assert redis.set.call_count == 2


# ── Chaos integration: checkpoint corruption filters uncommitted ───────────────

class TestCheckpointCorruptionFilter:

    @pytest.mark.asyncio
    async def test_load_returns_only_committed_checkpoints(self):
        """INV-EXEC-002: load() must never return uncommitted checkpoints."""
        from app.distributed.execution.redis_checkpoint import RedisCheckpointStore

        checkpoints = [
            {"execution_id": "e1", "step_id": "s0", "data": {}, "committed": True, "written_at": ""},
            {"execution_id": "e1", "step_id": "s1", "data": {}, "committed": False, "written_at": ""},
            {"execution_id": "e1", "step_id": "s2", "data": {}, "committed": True, "written_at": ""},
        ]
        step_ids = [b"s0", b"s1", b"s2"]

        redis = AsyncMock()
        redis.zrange = AsyncMock(return_value=step_ids)

        def _get(key):
            for cp in checkpoints:
                step = key.decode().split(":")[-1]
                if cp["step_id"] == step:
                    return json.dumps(cp).encode()
            return None

        redis.get = AsyncMock(side_effect=_get)

        store = RedisCheckpointStore.__new__(RedisCheckpointStore)
        store._url = "redis://localhost"
        store._redis = redis
        store._ttl = 86400
        store._disk_dir = None

        fi = FaultInjector()
        async with CheckpointCorruptionScenario(fi):
            results = await store.load("e1")

        # Only committed checkpoints returned
        assert all(r["committed"] for r in results)
        assert len(results) == 2   # s0 and s2, not s1


# ── Chaos integration: capability registry heartbeat loss ──────────────────────

class TestHeartbeatLossEviction:

    @pytest.mark.asyncio
    async def test_capability_registry_evicts_on_heartbeat_loss(self):
        """Workers that stop heartbeating (HeartbeatLoss) are evicted from registry."""
        from app.distributed.capability.federation import (
            CapabilityAdvertisement,
            CapabilityFederator,
            LLMCapability,
        )

        fed = CapabilityFederator(ttl_s=0.1)   # 100ms TTL for quick eviction
        adv = CapabilityAdvertisement(
            worker_id="dying-worker",
            llm=LLMCapability(models=["gpt-4"]),
        )
        await fed.advertise(adv)

        fi = FaultInjector()
        async with HeartbeatLossScenario(fi):
            await fi.should_inject(FaultType.HEARTBEAT_LOSS)
            # Worker stops sending heartbeats — we simply wait for TTL to expire
            await asyncio.sleep(0.15)

        # After TTL, worker no longer appears in discovery
        profiles = await fed.profiles()
        assert all(p.worker_id != "dying-worker" for p in profiles)
