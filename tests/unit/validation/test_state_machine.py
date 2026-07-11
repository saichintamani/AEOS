"""
Tests for Phase 9B.6 — StateMachineValidator (017-STATE_MACHINE_SPECIFICATION.md).
"""

from __future__ import annotations

import pytest

from app.distributed.validation.state_machine import (
    StateMachine,
    StateMachineValidator,
    StateMachineViolation,
    TransitionRecord,
)


# ── StateMachine (per-entity) ──────────────────────────────────────────────────

class TestStateMachineBasics:

    def test_initial_state(self):
        sm = StateMachine("SM-TASK", "PENDING")
        assert sm.state == "PENDING"

    def test_valid_transition_succeeds(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("SCHEDULED", event="dispatch")
        assert sm.state == "SCHEDULED"

    def test_invalid_transition_raises(self):
        sm = StateMachine("SM-TASK", "PENDING")
        with pytest.raises(StateMachineViolation) as exc_info:
            sm.transition("RUNNING", event="skip_scheduled")  # PENDING→RUNNING not allowed
        assert exc_info.value.machine == "SM-TASK"
        assert exc_info.value.from_state == "PENDING"
        assert exc_info.value.to_state == "RUNNING"

    def test_unknown_machine_raises(self):
        with pytest.raises(ValueError):
            StateMachine("SM-UNKNOWN-9999", "PENDING")

    def test_history_records_transitions(self):
        sm = StateMachine("SM-TASK", "PENDING", entity_id="task-1")
        sm.transition("SCHEDULED")
        sm.transition("RUNNING")   # SCHEDULED→RUNNING is valid
        assert len(sm.history) == 2
        assert sm.history[0].from_state == "PENDING"
        assert sm.history[0].to_state == "SCHEDULED"
        assert sm.history[0].valid is True

    def test_is_terminal_false_for_running(self):
        sm = StateMachine("SM-TASK", "RUNNING")
        assert sm.is_terminal is False

    def test_is_terminal_true_for_completed(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("SCHEDULED")
        sm.transition("RUNNING")
        sm.transition("COMPLETED")
        assert sm.is_terminal is True

    def test_can_transition_to(self):
        sm = StateMachine("SM-TASK", "PENDING")
        assert sm.can_transition_to("SCHEDULED") is True
        assert sm.can_transition_to("COMPLETED") is False

    def test_transition_from_terminal_raises(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("SCHEDULED")
        sm.transition("RUNNING")
        sm.transition("COMPLETED")
        with pytest.raises(StateMachineViolation):
            sm.transition("RUNNING")

    def test_entity_id_in_violation_context(self):
        sm = StateMachine("SM-TASK", "COMPLETED", entity_id="task-99")
        with pytest.raises(StateMachineViolation) as exc_info:
            sm.transition("RUNNING")
        assert "task-99" in str(exc_info.value.context)


# ── SM-TASK full lifecycle ────────────────────────────────────────────────────

class TestSMTask:

    def test_full_happy_path(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("SCHEDULED")
        sm.transition("RUNNING")
        sm.transition("COMPLETED")
        assert sm.state == "COMPLETED"
        assert sm.is_terminal

    def test_failed_path(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("SCHEDULED")
        sm.transition("RUNNING")
        sm.transition("FAILED")
        assert sm.is_terminal

    def test_cancelled_from_pending(self):
        sm = StateMachine("SM-TASK", "PENDING")
        sm.transition("CANCELLED")
        assert sm.is_terminal

    def test_suspend_and_resume(self):
        sm = StateMachine("SM-TASK", "RUNNING")
        sm.transition("SUSPENDED")
        sm.transition("RUNNING")
        assert sm.state == "RUNNING"

    def test_backward_transition_rejected(self):
        sm = StateMachine("SM-TASK", "RUNNING")
        with pytest.raises(StateMachineViolation):
            sm.transition("PENDING")

    def test_timeout_is_terminal(self):
        sm = StateMachine("SM-TASK", "RUNNING")
        sm.transition("TIMEOUT")
        assert sm.is_terminal
        with pytest.raises(StateMachineViolation):
            sm.transition("RUNNING")


# ── SM-CLUSTER-MEMBER ─────────────────────────────────────────────────────────

class TestSMClusterMember:

    def test_joining_to_running(self):
        sm = StateMachine("SM-CLUSTER-MEMBER", "JOINING")
        sm.transition("RUNNING")
        assert sm.state == "RUNNING"

    def test_running_to_suspected_to_running(self):
        sm = StateMachine("SM-CLUSTER-MEMBER", "RUNNING")
        sm.transition("SUSPECTED")
        sm.transition("RUNNING")
        assert sm.state == "RUNNING"

    def test_running_to_draining_to_left(self):
        sm = StateMachine("SM-CLUSTER-MEMBER", "RUNNING")
        sm.transition("DRAINING")
        sm.transition("LEFT")
        assert sm.is_terminal

    def test_failed_is_terminal(self):
        sm = StateMachine("SM-CLUSTER-MEMBER", "RUNNING")
        sm.transition("FAILED")
        assert sm.is_terminal

    def test_left_cannot_restart(self):
        sm = StateMachine("SM-CLUSTER-MEMBER", "DRAINING")
        sm.transition("LEFT")
        with pytest.raises(StateMachineViolation):
            sm.transition("JOINING")


# ── SM-CHECKPOINT ─────────────────────────────────────────────────────────────

class TestSMCheckpoint:

    def test_full_commit_path(self):
        sm = StateMachine("SM-CHECKPOINT", "PENDING")
        sm.transition("PHASE1_WRITTEN")
        sm.transition("COMMITTED")
        assert sm.is_terminal

    def test_failed_from_pending(self):
        sm = StateMachine("SM-CHECKPOINT", "PENDING")
        sm.transition("FAILED")
        assert sm.is_terminal

    def test_cannot_skip_phase1(self):
        sm = StateMachine("SM-CHECKPOINT", "PENDING")
        with pytest.raises(StateMachineViolation):
            sm.transition("COMMITTED")


# ── SM-CIRCUIT-BREAKER ────────────────────────────────────────────────────────

class TestSMCircuitBreaker:

    def test_closed_to_open(self):
        sm = StateMachine("SM-CIRCUIT-BREAKER", "CLOSED")
        sm.transition("OPEN")
        assert sm.state == "OPEN"

    def test_open_to_half_open_to_closed(self):
        sm = StateMachine("SM-CIRCUIT-BREAKER", "OPEN")
        sm.transition("HALF_OPEN")
        sm.transition("CLOSED")
        assert sm.state == "CLOSED"

    def test_open_to_half_open_to_open(self):
        sm = StateMachine("SM-CIRCUIT-BREAKER", "OPEN")
        sm.transition("HALF_OPEN")
        sm.transition("OPEN")
        assert sm.state == "OPEN"

    def test_closed_cannot_go_to_half_open_directly(self):
        sm = StateMachine("SM-CIRCUIT-BREAKER", "CLOSED")
        with pytest.raises(StateMachineViolation):
            sm.transition("HALF_OPEN")


# ── SM-RAFT ───────────────────────────────────────────────────────────────────

class TestSMRaft:

    def test_follower_to_candidate_to_leader(self):
        sm = StateMachine("SM-RAFT", "FOLLOWER")
        sm.transition("CANDIDATE")
        sm.transition("LEADER")
        assert sm.state == "LEADER"

    def test_leader_to_follower_on_higher_term(self):
        sm = StateMachine("SM-RAFT", "LEADER")
        sm.transition("FOLLOWER")
        assert sm.state == "FOLLOWER"

    def test_follower_cannot_become_leader_directly(self):
        sm = StateMachine("SM-RAFT", "FOLLOWER")
        with pytest.raises(StateMachineViolation):
            sm.transition("LEADER")

    def test_candidate_failed_election_returns_to_follower(self):
        sm = StateMachine("SM-RAFT", "CANDIDATE")
        sm.transition("FOLLOWER")
        assert sm.state == "FOLLOWER"


# ── StateMachineValidator (central service) ───────────────────────────────────

class TestStateMachineValidator:

    def test_valid_transition_returns_true(self):
        validator = StateMachineValidator()
        result = validator.validate("SM-TASK", "PENDING", "SCHEDULED")
        assert result is True

    def test_invalid_transition_raises(self):
        validator = StateMachineValidator()
        with pytest.raises(StateMachineViolation):
            validator.validate("SM-TASK", "COMPLETED", "RUNNING")

    def test_invalid_transition_no_raise(self):
        validator = StateMachineValidator()
        result = validator.validate("SM-TASK", "COMPLETED", "RUNNING",
                                   raise_on_violation=False)
        assert result is False

    def test_unknown_machine_raises_value_error(self):
        validator = StateMachineValidator()
        with pytest.raises((ValueError, StateMachineViolation)):
            validator.validate("SM-BOGUS", "A", "B")

    def test_violation_count_tracks_failures(self):
        validator = StateMachineValidator()
        validator.validate("SM-TASK", "PENDING", "SCHEDULED")  # ok
        validator.validate("SM-TASK", "COMPLETED", "RUNNING", raise_on_violation=False)  # bad
        assert validator.violation_count == 1

    def test_audit_log_contains_all_transitions(self):
        validator = StateMachineValidator()
        validator.validate("SM-TASK", "PENDING", "SCHEDULED")
        validator.validate("SM-TASK", "SCHEDULED", "RUNNING")
        log = validator.audit_log
        assert len(log) == 2
        assert all(isinstance(r, TransitionRecord) for r in log)

    def test_violations_returns_only_invalid(self):
        validator = StateMachineValidator()
        validator.validate("SM-TASK", "PENDING", "SCHEDULED")   # valid
        validator.validate("SM-TASK", "RUNNING", "PENDING", raise_on_violation=False)  # invalid
        assert len(validator.violations()) == 1
        assert validator.violations()[0].valid is False

    def test_is_terminal(self):
        validator = StateMachineValidator()
        assert validator.is_terminal("SM-TASK", "COMPLETED") is True
        assert validator.is_terminal("SM-TASK", "RUNNING") is False

    def test_allowed_transitions(self):
        validator = StateMachineValidator()
        allowed = validator.allowed_transitions("SM-TASK", "PENDING")
        assert "SCHEDULED" in allowed
        assert "CANCELLED" in allowed
        assert "RUNNING" not in allowed

    def test_machines_returns_all(self):
        machines = StateMachineValidator.machines()
        assert "SM-TASK" in machines
        assert "SM-RAFT" in machines
        assert "SM-CLUSTER-MEMBER" in machines
        assert "SM-WORKFLOW" in machines

    def test_context_passed_to_violation(self):
        validator = StateMachineValidator()
        with pytest.raises(StateMachineViolation) as exc:
            validator.validate(
                "SM-TASK", "COMPLETED", "RUNNING",
                context={"task_id": "t99"},
            )
        assert exc.value.context.get("task_id") == "t99"


# ── StateMachineViolation exception ──────────────────────────────────────────

class TestStateMachineViolationException:

    def test_str_repr(self):
        exc = StateMachineViolation(
            machine="SM-TASK",
            from_state="COMPLETED",
            to_state="RUNNING",
            event="retry",
        )
        s = str(exc)
        assert "SM-TASK" in s
        assert "COMPLETED" in s
        assert "RUNNING" in s

    def test_is_exception_instance(self):
        exc = StateMachineViolation(machine="SM-RAFT", from_state="A", to_state="B")
        assert isinstance(exc, Exception)
