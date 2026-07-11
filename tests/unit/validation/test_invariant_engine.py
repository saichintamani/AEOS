"""
Tests for Phase 9B.6 — InvariantEngine (019-INVARIANTS.md enforcement).
"""

from __future__ import annotations

import asyncio

import pytest

from app.distributed.validation.invariants import (
    InvariantCatalog,
    InvariantEngine,
    InvariantError,
    InvariantViolation,
    Severity,
    check_checkpoint_committed_only,
    check_governance_fail_closed,
    check_lease_held_before_execution,
    check_membership_cache_staleness,
    check_no_duplicate_execution,
    check_raft_inv_001,
    check_raft_log_monotonicity,
    check_raft_single_leader,
    check_redis_key_hashtags,
)


# ── InvariantViolation ────────────────────────────────────────────────────────

class TestInvariantViolation:

    def test_str_includes_id_and_message(self):
        v = InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "Duplicate execution")
        s = str(v)
        assert "INV-EXEC-001" in s
        assert "Duplicate execution" in s

    def test_str_includes_context(self):
        v = InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "msg",
                               context={"task_id": "t1"})
        assert "t1" in str(v)


# ── Catalog ───────────────────────────────────────────────────────────────────

class TestInvariantCatalog:

    def test_all_returns_nonempty_list(self):
        items = InvariantCatalog.all()
        assert len(items) > 0

    def test_get_known_invariant(self):
        meta = InvariantCatalog.get("INV-EXEC-001")
        assert meta is not None
        assert meta.invariant_id == "INV-EXEC-001"
        assert meta.severity == Severity.CRITICAL

    def test_get_unknown_returns_none(self):
        assert InvariantCatalog.get("INV-UNKNOWN-999") is None

    def test_by_category_execution(self):
        items = InvariantCatalog.by_category("execution")
        ids = {m.invariant_id for m in items}
        assert "INV-EXEC-001" in ids
        assert "INV-EXEC-002" in ids

    def test_critical_returns_only_critical(self):
        items = InvariantCatalog.critical()
        assert all(m.severity == Severity.CRITICAL for m in items)
        assert len(items) > 0


# ── Check: INV-CONS-001 (single leader per term) ──────────────────────────────

class TestCheckRaftSingleLeader:

    @pytest.mark.asyncio
    async def test_single_leader_passes(self):
        from app.distributed.consensus.raft import RaftNode, RaftRole
        from unittest.mock import AsyncMock

        nodes: dict = {}

        async def noop_rpc(nid, method, payload):
            pass

        for nid in ["n1", "n2", "n3"]:
            peers = [p for p in ["n1", "n2", "n3"] if p != nid]
            nodes[nid] = RaftNode(node_id=nid, peers=peers, rpc_send=noop_rpc)

        await nodes["n1"]._start_election()

        check = check_raft_single_leader(list(nodes.values()))
        violations = await check()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_two_leaders_same_term_detected(self):
        from app.distributed.consensus.raft import RaftNode, RaftRole
        from unittest.mock import AsyncMock

        nodes: dict = {}

        async def noop_rpc(nid, method, payload):
            pass

        for nid in ["n1", "n2", "n3"]:
            peers = [p for p in ["n1", "n2", "n3"] if p != nid]
            nodes[nid] = RaftNode(node_id=nid, peers=peers, rpc_send=noop_rpc)

        # Force both n1 and n2 to believe they are leader for term 1
        nodes["n1"]._role = RaftRole.LEADER
        nodes["n1"]._state.current_term = 1
        nodes["n2"]._role = RaftRole.LEADER
        nodes["n2"]._state.current_term = 1

        check = check_raft_single_leader(list(nodes.values()))
        violations = await check()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-CONS-001"
        assert violations[0].severity == Severity.CRITICAL


# ── Check: INV-CONS-002 (log monotonicity) ────────────────────────────────────

class TestCheckRaftLogMonotonicity:

    @pytest.mark.asyncio
    async def test_valid_log_passes(self):
        from app.distributed.consensus.raft import RaftNode

        async def noop(nid, method, payload): pass
        node = RaftNode("n1", ["n2"], noop)
        await node._start_election()
        await node.propose({"op": "a"})
        await node.propose({"op": "b"})

        check = check_raft_log_monotonicity([node])
        violations = await check()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_last_applied_exceeds_commit_detected(self):
        from app.distributed.consensus.raft import RaftNode

        async def noop(nid, method, payload): pass
        node = RaftNode("n1", ["n2"], noop)

        # Manually corrupt: set last_applied ahead of commit_index
        node._state.last_applied = 10
        node._state.commit_index = 5

        check = check_raft_log_monotonicity([node])
        violations = await check()
        assert len(violations) >= 1
        assert violations[0].invariant_id == "INV-CONS-002"


# ── Check: INV-EXEC-002 (checkpoint committed only) ──────────────────────────

class TestCheckCheckpointCommittedOnly:

    @pytest.mark.asyncio
    async def test_all_committed_passes(self):
        data = [
            {"execution_id": "e1", "step_id": "s0", "committed": True},
            {"execution_id": "e1", "step_id": "s1", "committed": True},
        ]
        check = check_checkpoint_committed_only(data)
        violations = await check()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_uncommitted_checkpoint_detected(self):
        data = [
            {"execution_id": "e1", "step_id": "s0", "committed": True},
            {"execution_id": "e1", "step_id": "s1", "committed": False},
        ]
        check = check_checkpoint_committed_only(data)
        violations = await check()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-EXEC-002"
        assert violations[0].context["step_id"] == "s1"


# ── Check: INV-EXEC-004 (lease held before execution) ─────────────────────────

class TestCheckLeaseHeld:

    @pytest.mark.asyncio
    async def test_lease_holder_matches_passes(self):
        tasks = [
            {"task_id": "t1", "worker_id": "w1", "lease_holder": "w1"},
        ]
        violations = await check_lease_held_before_execution(tasks)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_lease_holder_mismatch_detected(self):
        tasks = [
            {"task_id": "t1", "worker_id": "w1", "lease_holder": "w2"},
        ]
        violations = await check_lease_held_before_execution(tasks)()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-EXEC-004"
        assert violations[0].severity == Severity.CRITICAL


# ── Check: INV-EXEC-001 (no duplicate execution) ──────────────────────────────

class TestCheckNoDuplicateExecution:

    @pytest.mark.asyncio
    async def test_single_execution_passes(self):
        records = [{"task_id": "t1", "step_id": "s1", "execute_count": 1, "max_retries": 0}]
        violations = await check_no_duplicate_execution(records)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_duplicate_with_no_retry_detected(self):
        records = [{"task_id": "t1", "step_id": "s1", "execute_count": 2, "max_retries": 0}]
        violations = await check_no_duplicate_execution(records)()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-EXEC-001"

    @pytest.mark.asyncio
    async def test_duplicate_with_retry_allowed(self):
        records = [{"task_id": "t1", "step_id": "s1", "execute_count": 2, "max_retries": 3}]
        violations = await check_no_duplicate_execution(records)()
        assert len(violations) == 0


# ── Check: INV-EXEC-005 (fail-closed governance) ──────────────────────────────

class TestCheckGovernanceFailClosed:

    @pytest.mark.asyncio
    async def test_approved_with_policy_passes(self):
        evals = [{"task_type": "research", "result": "APPROVED",
                  "had_matching_policy": True, "timed_out": False}]
        violations = await check_governance_fail_closed(evals)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_approved_without_policy_detected(self):
        evals = [{"task_type": "unknown", "result": "APPROVED",
                  "had_matching_policy": False, "timed_out": False}]
        violations = await check_governance_fail_closed(evals)()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-EXEC-005"

    @pytest.mark.asyncio
    async def test_approved_on_timeout_detected(self):
        evals = [{"task_type": "research", "result": "APPROVED",
                  "had_matching_policy": True, "timed_out": True}]
        violations = await check_governance_fail_closed(evals)()
        assert len(violations) == 1

    @pytest.mark.asyncio
    async def test_rejected_without_policy_passes(self):
        evals = [{"task_type": "unknown", "result": "REJECTED",
                  "had_matching_policy": False, "timed_out": False}]
        violations = await check_governance_fail_closed(evals)()
        assert len(violations) == 0


# ── Check: INV-CONS-004 (membership cache staleness) ─────────────────────────

class TestCheckMembershipStaleness:

    @pytest.mark.asyncio
    async def test_consistent_membership_passes(self):
        raft = {"w1", "w2", "w3"}
        cache = {"w1", "w2", "w3"}
        violations = await check_membership_cache_staleness(raft, cache, cache_age_s=0.5)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_stale_diverged_membership_detected(self):
        raft = {"w1", "w2", "w3"}
        cache = {"w1", "w2"}  # w3 missing
        violations = await check_membership_cache_staleness(
            raft, cache, max_staleness_s=5.0, cache_age_s=6.0
        )()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-CONS-004"

    @pytest.mark.asyncio
    async def test_diverged_but_within_staleness_window_passes(self):
        raft = {"w1", "w2", "w3"}
        cache = {"w1", "w2"}  # diverged but only 2s old
        violations = await check_membership_cache_staleness(
            raft, cache, max_staleness_s=5.0, cache_age_s=2.0
        )()
        assert len(violations) == 0


# ── Check: INV-MEM-001 (Redis key hashtags) ───────────────────────────────────

class TestCheckRedisKeyHashtags:

    @pytest.mark.asyncio
    async def test_same_hashtag_passes(self):
        groups = [
            ["{wf:w1}:step:s1", "{wf:w1}:idem:s1", "{wf:w1}:result:s1"],
        ]
        violations = await check_redis_key_hashtags(groups)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_different_hashtags_detected(self):
        groups = [
            ["{wf:w1}:step:s1", "{wf:w2}:step:s2"],   # two different workflows in one MULTI
        ]
        violations = await check_redis_key_hashtags(groups)()
        assert len(violations) == 1
        assert violations[0].invariant_id == "INV-MEM-001"

    @pytest.mark.asyncio
    async def test_keys_without_hashtags_ignored(self):
        groups = [
            ["bare_key_1", "bare_key_2"],   # no hashtags — OK
        ]
        violations = await check_redis_key_hashtags(groups)()
        assert len(violations) == 0

    @pytest.mark.asyncio
    async def test_multiple_groups_evaluated_independently(self):
        groups = [
            ["{wf:w1}:a", "{wf:w1}:b"],         # ok
            ["{wf:w1}:c", "{wf:w2}:d"],         # violation
        ]
        violations = await check_redis_key_hashtags(groups)()
        assert len(violations) == 1


# ── InvariantEngine ───────────────────────────────────────────────────────────

class TestInvariantEngine:

    @pytest.mark.asyncio
    async def test_register_and_evaluate(self):
        engine = InvariantEngine()

        async def _passing() -> list:
            return []

        engine.register("INV-EXEC-001", _passing)
        result = await engine.evaluate()
        assert result.ok
        assert "INV-EXEC-001" in result.passed

    @pytest.mark.asyncio
    async def test_evaluate_returns_violations(self):
        engine = InvariantEngine()

        async def _failing():
            return [InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "test")]

        engine.register("INV-EXEC-001", _failing)
        result = await engine.evaluate()
        assert not result.ok
        assert len(result.violations) == 1

    @pytest.mark.asyncio
    async def test_evaluate_subset_of_ids(self):
        engine = InvariantEngine()
        calls = []

        async def _a():
            calls.append("a")
            return []

        async def _b():
            calls.append("b")
            return []

        engine.register("INV-EXEC-001", _a)
        engine.register("INV-EXEC-002", _b)
        await engine.evaluate(invariant_ids=["INV-EXEC-001"])
        assert "a" in calls
        assert "b" not in calls

    @pytest.mark.asyncio
    async def test_raise_on_critical(self):
        engine = InvariantEngine()

        async def _crit():
            return [InvariantViolation("INV-CONS-001", Severity.CRITICAL, "split brain")]

        engine.register("INV-CONS-001", _crit)
        with pytest.raises(InvariantError) as exc_info:
            await engine.evaluate(raise_on_critical=True)
        assert "INV-CONS-001" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_violation_callback_fired(self):
        engine = InvariantEngine()
        received = []
        engine.on_violation(lambda v: received.append(v))

        async def _fail():
            return [InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "dup")]

        engine.register("INV-EXEC-001", _fail)
        await engine.evaluate()
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_stats_track_evaluations(self):
        engine = InvariantEngine()

        async def _ok(): return []
        engine.register("INV-EXEC-001", _ok)

        await engine.evaluate()
        await engine.evaluate()
        assert engine.stats["total_evaluations"] == 2

    @pytest.mark.asyncio
    async def test_background_monitor_start_stop(self):
        engine = InvariantEngine(check_interval_s=100.0)  # never fires in test
        await engine.start()
        assert engine._running is True
        await engine.stop()
        assert engine._running is False

    @pytest.mark.asyncio
    async def test_violation_history_accumulates(self):
        engine = InvariantEngine()

        async def _fail():
            return [InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "dup")]

        engine.register("INV-EXEC-001", _fail)
        await engine.evaluate()
        await engine.evaluate()
        assert len(engine.violation_history) == 2

    @pytest.mark.asyncio
    async def test_result_critical_violations_property(self):
        engine = InvariantEngine()

        async def _mixed():
            return [
                InvariantViolation("INV-EXEC-001", Severity.CRITICAL, "crit"),
                InvariantViolation("INV-CONS-004", Severity.ERROR, "err"),
            ]

        engine.register("INV-EXEC-001", _mixed)
        result = await engine.evaluate()
        assert len(result.critical_violations) == 1
        assert result.critical_violations[0].invariant_id == "INV-EXEC-001"
