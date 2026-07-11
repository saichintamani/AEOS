"""
Fault injection tests — verifies recovery invariants under controlled failures.

Each test arms a fault scenario, runs an execution, and asserts that the
expected recovery invariants hold after the fault is triggered.

Architecture Contract IDs: AC-EXEC-001, AC-CONS-001
Invariant IDs: INV-EXEC-001, INV-EXEC-002
Protocol IDs: PROTO-009, PROTO-019
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.context import ExecutionContext, CheckpointData
from app.distributed.execution.lease import ExecutionLeaseManager, StaleFencingTokenError
from app.distributed.execution.recovery import RecoveryRuntime
from app.distributed.execution.states import ExecutionState
from app.distributed.fault.injector import FaultInjector, FaultType, VerificationResult
from app.distributed.fault.scenarios import (
    WorkerCrashScenario,
    LeaseExpirationScenario,
    DuplicateEventScenario,
)


class TestFaultInjector:

    @pytest.mark.asyncio
    async def test_arm_and_trigger(self):
        injector = FaultInjector()
        injector.arm(FaultType.WORKER_CRASH, after_count=2, trigger_count=1)

        # First two calls should not trigger
        assert not await injector.should_inject(FaultType.WORKER_CRASH)
        assert not await injector.should_inject(FaultType.WORKER_CRASH)

        # Third call triggers
        assert await injector.should_inject(FaultType.WORKER_CRASH)

        # Fourth call — trigger_count exhausted
        assert not await injector.should_inject(FaultType.WORKER_CRASH)

    @pytest.mark.asyncio
    async def test_triggered_flag(self):
        injector = FaultInjector()
        injector.arm(FaultType.NETWORK_DELAY, trigger_count=1)
        assert not injector.triggered(FaultType.NETWORK_DELAY)
        await injector.should_inject(FaultType.NETWORK_DELAY)
        assert injector.triggered(FaultType.NETWORK_DELAY)

    @pytest.mark.asyncio
    async def test_verifier_passes(self):
        injector = FaultInjector()

        async def passing_verifier() -> VerificationResult:
            return VerificationResult(passed=True, invariant_ids=["INV-EXEC-001"])

        injector.register_verifier(passing_verifier)
        results = await injector.verify_all()
        assert all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_verifier_failure(self):
        injector = FaultInjector()

        async def failing_verifier() -> VerificationResult:
            return VerificationResult(
                passed=False,
                violations=["Duplicate execution detected"],
            )

        injector.register_verifier(failing_verifier)
        results = await injector.verify_all()
        assert not all(r.passed for r in results)

    @pytest.mark.asyncio
    async def test_reset_clears_state(self):
        injector = FaultInjector()
        injector.arm(FaultType.WORKER_CRASH, trigger_count=1)
        await injector.should_inject(FaultType.WORKER_CRASH)
        injector.reset()
        assert not injector.triggered(FaultType.WORKER_CRASH)
        assert injector.trigger_log == []


class TestWorkerCrashScenario:

    @pytest.mark.asyncio
    async def test_crash_scenario_arms_correct_fault(self):
        injector = FaultInjector()
        scenario = WorkerCrashScenario(injector, crash_after_tasks=0)
        async with scenario:
            assert await injector.should_inject(FaultType.WORKER_CRASH)
        assert scenario.result is not None
        assert "INV-EXEC-001" in scenario.result.invariant_ids

    @pytest.mark.asyncio
    async def test_scenario_recovery_verification(self):
        """Full crash → recovery verification flow."""
        lease_store = InMemoryLeaseStore()
        cp_store = InMemoryCheckpointStore()
        lease_mgr = ExecutionLeaseManager(lease_store)
        cp_engine = CheckpointEngine(cp_store)
        recovery = RecoveryRuntime(lease_mgr, cp_engine, worker_id="recovery")

        injector = FaultInjector()

        # Register an invariant verifier: after recovery, no lease should be
        # held by the crashed worker
        async def inv_exec_001() -> VerificationResult:
            record = await lease_store.get("exec:wf-crash:step-1")
            if record and record.holder_id == "crashed-worker":
                return VerificationResult(
                    passed=False,
                    invariant_ids=["INV-EXEC-001"],
                    violations=["Crashed worker still holds lease"],
                )
            return VerificationResult(passed=True, invariant_ids=["INV-EXEC-001"])

        injector.register_verifier(inv_exec_001)
        scenario = WorkerCrashScenario(injector, crash_after_tasks=0)

        async with scenario:
            # Simulate crash: acquire then immediately release (crash simulation)
            token = await lease_mgr.acquire("exec:wf-crash:step-1", "crashed-worker")
            # Write a checkpoint
            cp_data = CheckpointData(
                task_id="t-crash", workflow_id="wf-crash", step_id="step-1",
                execution_id="exec-1", step_index=3,
            )
            entry = await cp_engine.write_full(cp_data)
            await cp_engine.commit(entry)
            # "Crash" — release the lease
            await lease_mgr.release(token)

            # Recovery takes over
            ctx = ExecutionContext(
                task_id="t-crash", workflow_id="wf-crash", step_id="step-1",
                execution_id="exec-1",
                state=ExecutionState.RUNNING,
                assigned_worker_id="crashed-worker",
                lease_key="exec:wf-crash:step-1",
            )
            result = await recovery.recover(ctx)
            assert result.success

        assert scenario.result.passed


class TestStaleFencingTokenInvariant:
    """INV-EXEC-001: stale fencing token prevents double execution."""

    @pytest.mark.asyncio
    async def test_stolen_lease_prevents_original_checkpoint(self):
        lease_store = InMemoryLeaseStore()
        lease_mgr = ExecutionLeaseManager(lease_store)

        # Worker A acquires lease
        token_a = await lease_mgr.acquire("exec:wf1:s1", "worker-a")
        assert token_a is not None

        # Recovery worker steals lease (worker A crashed)
        stolen = await lease_mgr.steal("exec:wf1:s1", "recovery-worker")
        assert stolen is not None

        # Worker A's token should now be invalid
        valid = await lease_mgr.verify(token_a)
        assert not valid, "Stale token must be rejected (INV-EXEC-001)"

        # Worker A attempting to release with stale token raises
        with pytest.raises(StaleFencingTokenError):
            await lease_mgr.release(token_a)


class TestCheckpointOrdering:
    """INV-EXEC-002: checkpoint always precedes offset commit."""

    @pytest.mark.asyncio
    async def test_uncommitted_checkpoint_not_visible(self):
        """
        A checkpoint written in Phase 1 but not Phase 2 should NOT be returned
        as the latest checkpoint (simulates crash between phases).
        """
        cp_store = InMemoryCheckpointStore()
        cp_engine = CheckpointEngine(cp_store)

        data = CheckpointData(
            task_id="t1", workflow_id="wf1", step_id="s1",
            execution_id="e1", sequence_number=1,
        )
        # Phase 1 only — no commit
        await cp_engine.write_full(data)

        # Recovery should NOT find this checkpoint (uncommitted)
        loaded = await cp_engine.load("wf1", "s1")
        assert loaded is None, "Uncommitted checkpoint must not be used (INV-EXEC-002)"

    @pytest.mark.asyncio
    async def test_committed_checkpoint_survives_restart(self):
        """
        A committed checkpoint must be available after simulated restart.
        """
        cp_store = InMemoryCheckpointStore()
        cp_engine = CheckpointEngine(cp_store)

        data = CheckpointData(
            task_id="t1", workflow_id="wf1", step_id="s1",
            execution_id="e1", sequence_number=1,
            task_state={"result": 42},
        )
        entry = await cp_engine.write_full(data)
        await cp_engine.commit(entry)  # Phase 2

        # Simulated restart: new engine with same store
        engine2 = CheckpointEngine(cp_store)
        loaded = await engine2.load("wf1", "s1")
        assert loaded is not None
        assert loaded.task_state["result"] == 42
