"""
Unit tests — SM-TASK transitions, ExecutionContext, FencingToken,
CheckpointEngine (PROTO-008), RecoveryRuntime (PROTO-009), TaskExecutionEngine.

Contract: AC-EXEC-001, AC-EXEC-002
"""

from __future__ import annotations

import asyncio
import pytest

from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.context import CheckpointData, ExecutionContext
from app.distributed.execution.engine import TaskExecutionEngine
from app.distributed.execution.lease import ExecutionLeaseManager, StaleFencingTokenError
from app.distributed.execution.recovery import RecoveryRuntime
from app.distributed.execution.states import (
    ExecutionState,
    InvalidTransitionError,
    validate_execution_transition,
)


class TestStateMachine:

    def test_valid_transitions(self):
        for from_s, to_s in [
            (ExecutionState.CREATED,      ExecutionState.QUEUED),
            (ExecutionState.QUEUED,       ExecutionState.LEASED),
            (ExecutionState.LEASED,       ExecutionState.SCHEDULED),
            (ExecutionState.SCHEDULED,    ExecutionState.RUNNING),
            (ExecutionState.RUNNING,      ExecutionState.COMPLETED),
            (ExecutionState.RUNNING,      ExecutionState.FAILED),
            (ExecutionState.FAILED,       ExecutionState.QUEUED),
        ]:
            t = validate_execution_transition(from_s, to_s)
            assert t.from_state == from_s
            assert t.to_state == to_s

    def test_invalid_transition_raises(self):
        with pytest.raises(InvalidTransitionError):
            validate_execution_transition(ExecutionState.COMPLETED, ExecutionState.RUNNING)

    def test_cancelled_is_terminal(self):
        with pytest.raises(InvalidTransitionError):
            validate_execution_transition(ExecutionState.CANCELLED, ExecutionState.QUEUED)


class TestExecutionContext:

    def test_transition_updates_state(self):
        ctx = ExecutionContext(workflow_id="wf", step_id="s1")
        ctx.transition(ExecutionState.QUEUED)
        assert ctx.state == ExecutionState.QUEUED
        assert ctx.queued_at is not None

    def test_can_retry(self):
        ctx = ExecutionContext(attempt=0, max_attempts=3)
        assert ctx.can_retry()
        ctx.attempt = 3
        assert not ctx.can_retry()

    def test_lease_key_for(self):
        ctx = ExecutionContext(workflow_id="wf-1", step_id="step-1")
        assert ctx.lease_key_for() == "exec:wf-1:step-1"

    def test_to_checkpoint(self):
        ctx = ExecutionContext(
            task_id="t1", workflow_id="wf", step_id="s1",
            assigned_worker_id="w1",
        )
        ctx.transition(ExecutionState.QUEUED)
        ctx.transition(ExecutionState.LEASED)
        ctx.transition(ExecutionState.SCHEDULED)
        ctx.transition(ExecutionState.RUNNING)
        cp = ctx.to_checkpoint(step_index=3, total_steps=10)
        assert cp.step_index == 3
        assert cp.total_steps == 10
        assert cp.workflow_id == "wf"


class TestFencingToken:

    @pytest.mark.asyncio
    async def test_acquire_returns_token(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        token = await mgr.acquire("k", "h1")
        assert token is not None
        assert token.value == 1
        assert token.holder_id == "h1"

    @pytest.mark.asyncio
    async def test_second_acquire_fails(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        await mgr.acquire("k", "h1")
        t2 = await mgr.acquire("k", "h2")
        assert t2 is None

    @pytest.mark.asyncio
    async def test_verify_valid_token(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        token = await mgr.acquire("k", "h1")
        assert await mgr.verify(token)

    @pytest.mark.asyncio
    async def test_stale_token_after_steal(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        token_a = await mgr.acquire("k", "h1")
        await mgr.steal("k", "h2")
        assert not await mgr.verify(token_a)

    @pytest.mark.asyncio
    async def test_release_stale_raises(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        token_a = await mgr.acquire("k", "h1")
        await mgr.steal("k", "h2")
        with pytest.raises(StaleFencingTokenError):
            await mgr.release(token_a)


class TestCheckpointEngine:

    @pytest.mark.asyncio
    async def test_uncommitted_not_visible(self):
        """INV-EXEC-002: uncommitted checkpoint must not be returned by load()."""
        store = InMemoryCheckpointStore()
        engine = CheckpointEngine(store)
        data = CheckpointData(workflow_id="wf", step_id="s1", execution_id="e1")
        await engine.write_full(data)  # Phase 1 only
        loaded = await engine.load("wf", "s1")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_committed_visible(self):
        """Committed checkpoint is returned by load()."""
        store = InMemoryCheckpointStore()
        engine = CheckpointEngine(store)
        data = CheckpointData(workflow_id="wf", step_id="s1", execution_id="e1",
                              task_state={"k": 42})
        entry = await engine.write_full(data)
        await engine.commit(entry)
        loaded = await engine.load("wf", "s1")
        assert loaded is not None
        assert loaded.task_state["k"] == 42

    @pytest.mark.asyncio
    async def test_survives_engine_restart(self):
        store = InMemoryCheckpointStore()
        engine1 = CheckpointEngine(store)
        data = CheckpointData(workflow_id="wf", step_id="s1", execution_id="e1",
                              task_state={"x": 99})
        entry = await engine1.write_full(data)
        await engine1.commit(entry)

        engine2 = CheckpointEngine(store)
        loaded = await engine2.load("wf", "s1")
        assert loaded.task_state["x"] == 99


class TestRecoveryRuntime:

    @pytest.mark.asyncio
    async def test_pattern_a_no_checkpoint(self):
        mgr = ExecutionLeaseManager(InMemoryLeaseStore())
        cp_engine = CheckpointEngine(InMemoryCheckpointStore())
        recovery = RecoveryRuntime(mgr, cp_engine)

        ctx = ExecutionContext(
            workflow_id="wf", step_id="s1",
            state=ExecutionState.RUNNING,
            assigned_worker_id="crashed",
            lease_key="exec:wf:s1",
            attempt=0, max_attempts=3,
        )
        result = await recovery.recover(ctx)
        assert result.success
        assert result.pattern == "A"
        assert ctx.state == ExecutionState.QUEUED
        assert ctx.attempt == 1

    @pytest.mark.asyncio
    async def test_pattern_c_resume_from_checkpoint(self):
        store = InMemoryLeaseStore()
        mgr = ExecutionLeaseManager(store)
        cp_store = InMemoryCheckpointStore()
        cp_engine = CheckpointEngine(cp_store)
        recovery = RecoveryRuntime(mgr, cp_engine, worker_id="recovery")

        token = await mgr.acquire("exec:wf:s1", "crashed")
        data = CheckpointData(
            task_id="t1", workflow_id="wf", step_id="s1",
            execution_id="e1", step_index=5,
        )
        entry = await cp_engine.write_full(data)
        await cp_engine.commit(entry)
        await mgr.release(token)

        ctx = ExecutionContext(
            task_id="t1", workflow_id="wf", step_id="s1",
            state=ExecutionState.RUNNING,
            assigned_worker_id="crashed",
            lease_key="exec:wf:s1",
        )
        ctx.execution_id = "e1"

        result = await recovery.recover(ctx)
        assert result.success
        assert result.pattern == "C"
        assert ctx.state == ExecutionState.RECOVERING
        assert result.checkpoint.step_index == 5
        assert result.token is not None


class TestTaskExecutionEngine:

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        lease_store = InMemoryLeaseStore()
        cp_store = InMemoryCheckpointStore()
        engine = TaskExecutionEngine(
            ExecutionLeaseManager(lease_store),
            CheckpointEngine(cp_store),
        )

        async def my_handler(ctx, callbacks):
            return {"done": True}

        engine.register_handler("echo", my_handler)

        ctx = ExecutionContext(
            task_id="t1", workflow_id="wf", step_id="s1",
            task_type="echo", assigned_worker_id="w1",
        )
        ctx.transition(ExecutionState.QUEUED)
        ctx.transition(ExecutionState.LEASED)

        result = await engine.execute(ctx)
        assert result.success
        assert result.result["done"] is True
        assert ctx.state == ExecutionState.COMPLETED

    @pytest.mark.asyncio
    async def test_no_handler_fails(self):
        engine = TaskExecutionEngine(
            ExecutionLeaseManager(InMemoryLeaseStore()),
            CheckpointEngine(InMemoryCheckpointStore()),
        )
        ctx = ExecutionContext(
            task_type="unknown", assigned_worker_id="w1",
            workflow_id="wf", step_id="s1",
        )
        ctx.transition(ExecutionState.QUEUED)
        ctx.transition(ExecutionState.LEASED)
        result = await engine.execute(ctx)
        assert not result.success
