"""
Tests for Phase 9B.6 — ProtocolValidator (016-PROTOCOL_SPECIFICATION.md).
"""

from __future__ import annotations

import pytest

from app.distributed.validation.protocol import (
    ProtocolStep,
    ProtocolTrace,
    ProtocolValidator,
    ProtocolViolation,
)


# ── ProtocolTrace ─────────────────────────────────────────────────────────────

class TestProtocolTrace:

    def test_record_and_step_names(self):
        trace = ProtocolTrace(protocol_id="PROTO-008", execution_id="e1")
        trace.record("phase1_write", "worker")
        trace.record("phase2_commit", "worker")
        assert trace.step_names() == ["phase1_write", "phase2_commit"]

    def test_has_step_true(self):
        trace = ProtocolTrace("PROTO-008")
        trace.record("phase1_write")
        assert trace.has_step("phase1_write") is True

    def test_has_step_false(self):
        trace = ProtocolTrace("PROTO-008")
        assert trace.has_step("phase2_commit") is False

    def test_find_returns_step(self):
        trace = ProtocolTrace("PROTO-008")
        trace.record("phase1_write", "worker", payload={"step_id": "s1"})
        step = trace.find("phase1_write")
        assert step is not None
        assert step.payload["step_id"] == "s1"

    def test_find_returns_none_for_missing(self):
        trace = ProtocolTrace("PROTO-008")
        assert trace.find("nonexistent") is None

    def test_successful_steps_filters(self):
        trace = ProtocolTrace("PROTO-008")
        trace.record("phase1_write", success=True)
        trace.record("phase2_commit", success=False)
        assert len(trace.successful_steps()) == 1


# ── PROTO-008 (Two-Phase Checkpoint) ─────────────────────────────────────────

class TestProto008:

    def _valid_trace(self) -> ProtocolTrace:
        trace = ProtocolTrace("PROTO-008", "exec-1")
        trace.record("phase1_write", "worker")
        trace.record("phase2_commit", "worker")
        trace.record("offset_committed", "worker")
        return trace

    def test_valid_checkpoint_protocol_passes(self):
        validator = ProtocolValidator()
        violations = validator.validate(self._valid_trace())
        assert len(violations) == 0

    def test_missing_phase1_write_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-008", "exec-2")
        trace.record("phase2_commit", "worker")
        trace.record("offset_committed", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P008-R001" for v in violations)

    def test_missing_phase2_commit_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-008", "exec-3")
        trace.record("phase1_write", "worker")
        trace.record("offset_committed", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P008-R002" for v in violations)

    def test_offset_before_phase2_detected(self):
        """Offset committed BEFORE phase2 is a protocol violation."""
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-008", "exec-4")
        trace.record("phase1_write", "worker")
        trace.record("offset_committed", "worker")   # too early
        trace.record("phase2_commit", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P008-R004" for v in violations)

    def test_failed_phase1_write_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-008", "exec-5")
        trace.record("phase1_write", "worker", success=False)
        trace.record("phase2_commit", "worker")
        trace.record("offset_committed", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P008-R005" for v in violations)


# ── PROTO-006 (Task Dispatch) ─────────────────────────────────────────────────

class TestProto006:

    def _valid_trace(self) -> ProtocolTrace:
        trace = ProtocolTrace("PROTO-006", "task-1")
        trace.record("lease_acquired", "worker")
        trace.record("task_accepted_event", "coordinator")
        trace.record("execution_started", "worker")
        trace.record("phase1_checkpoint", "worker")
        trace.record("phase2_checkpoint", "worker")
        trace.record("task_completed_event", "worker")
        trace.record("offset_committed", "worker")
        return trace

    def test_valid_dispatch_protocol_passes(self):
        validator = ProtocolValidator()
        violations = validator.validate(self._valid_trace())
        assert len(violations) == 0

    def test_lease_acquired_required(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-006", "task-2")
        trace.record("task_accepted_event", "coordinator")
        trace.record("execution_started", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P006-R001" for v in violations)

    def test_execution_before_lease_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-006", "task-3")
        trace.record("execution_started", "worker")  # execution first
        trace.record("lease_acquired", "worker")      # lease after
        trace.record("task_accepted_event", "coordinator")
        trace.record("phase1_checkpoint", "worker")
        trace.record("phase2_checkpoint", "worker")
        trace.record("offset_committed", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P006-R004" for v in violations)

    def test_offset_before_phase2_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-006", "task-4")
        trace.record("lease_acquired", "worker")
        trace.record("task_accepted_event", "coordinator")
        trace.record("execution_started", "worker")
        trace.record("phase1_checkpoint", "worker")
        trace.record("offset_committed", "worker")   # offset before phase2
        trace.record("phase2_checkpoint", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P006-R006" for v in violations)


# ── PROTO-019 (Execution Lease Acquisition) ───────────────────────────────────

class TestProto019:

    def _valid_trace(self) -> ProtocolTrace:
        trace = ProtocolTrace("PROTO-019", "lease-1")
        trace.record("setnx_attempt", "worker")
        trace.record("setnx_result", "redis")
        trace.record("setnx_success", "redis")
        trace.record("fencing_token_incremented", "redis")
        trace.record("execution_started", "worker")
        return trace

    def test_valid_lease_acquisition_passes(self):
        validator = ProtocolValidator()
        violations = validator.validate(self._valid_trace())
        assert len(violations) == 0

    def test_setnx_attempt_required(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-019", "lease-2")
        trace.record("setnx_result", "redis")
        trace.record("execution_started", "worker")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P019-R001" for v in violations)

    def test_execution_without_fencing_token_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-019", "lease-3")
        trace.record("setnx_attempt", "worker")
        trace.record("setnx_result", "redis")
        trace.record("setnx_success", "redis")
        trace.record("execution_started", "worker")
        # No fencing_token_incremented!
        violations = validator.validate(trace)
        assert any(v.rule_id == "P019-R003" for v in violations)

    def test_execution_after_setnx_failed_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-019", "lease-4")
        trace.record("setnx_attempt", "worker")
        trace.record("setnx_result", "redis")
        trace.record("setnx_failed", "redis")       # lease NOT acquired
        trace.record("execution_started", "worker") # but execution started anyway!
        violations = validator.validate(trace)
        assert any(v.rule_id == "P019-R004" for v in violations)


# ── PROTO-009 (Checkpoint Recovery) ──────────────────────────────────────────

class TestProto009:

    def test_valid_requeue_path(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-009", "orphan-1")
        trace.record("orphan_detected", "scanner")
        trace.record("checkpoint_loaded", "scanner")
        trace.record("task_requeued", "scanner")
        violations = validator.validate(trace)
        assert len(violations) == 0

    def test_valid_resume_path(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-009", "orphan-2")
        trace.record("orphan_detected", "scanner")
        trace.record("checkpoint_loaded", "scanner")
        trace.record("task_resumed", "worker")
        violations = validator.validate(trace)
        assert len(violations) == 0

    def test_both_requeue_and_resume_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-009", "orphan-3")
        trace.record("orphan_detected", "scanner")
        trace.record("checkpoint_loaded", "scanner")
        trace.record("task_requeued", "scanner")    # both!
        trace.record("task_resumed", "worker")      # both!
        violations = validator.validate(trace)
        assert any(v.rule_id == "P009-R004" for v in violations)

    def test_missing_orphan_detected_step(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-009", "orphan-4")
        trace.record("checkpoint_loaded", "scanner")
        trace.record("task_requeued", "scanner")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P009-R001" for v in violations)


# ── PROTO-001 (Node Join) ─────────────────────────────────────────────────────

class TestProto001:

    def _valid_trace(self) -> ProtocolTrace:
        trace = ProtocolTrace("PROTO-001", "join-1")
        trace.record("join_request", "worker")
        trace.record("raft_log_append", "coordinator")
        trace.record("capability_registered", "coordinator")
        trace.record("join_response", "coordinator")
        return trace

    def test_valid_join_passes(self):
        validator = ProtocolValidator()
        violations = validator.validate(self._valid_trace())
        assert len(violations) == 0

    def test_capability_before_raft_detected(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-001", "join-2")
        trace.record("join_request", "worker")
        trace.record("capability_registered", "coordinator")  # before raft append
        trace.record("raft_log_append", "coordinator")
        trace.record("join_response", "coordinator")
        violations = validator.validate(trace)
        assert any(v.rule_id == "P001-R006" for v in violations)


# ── ProtocolValidator ─────────────────────────────────────────────────────────

class TestProtocolValidatorService:

    def test_unknown_protocol_returns_empty(self):
        validator = ProtocolValidator()
        trace = ProtocolTrace("PROTO-999", "unknown-1")
        trace.record("some_step")
        violations = validator.validate(trace)
        assert len(violations) == 0   # no rules = no violations

    def test_validate_all(self):
        validator = ProtocolValidator()
        t1 = ProtocolTrace("PROTO-008", "e1")
        t1.record("phase1_write")
        t1.record("phase2_commit")
        t1.record("offset_committed")

        t2 = ProtocolTrace("PROTO-008", "e2")
        t2.record("phase1_write")
        # missing phase2_commit and offset_committed

        violations = validator.validate_all([t1, t2])
        # t1 passes; t2 fails
        assert any(v.protocol_id == "PROTO-008" for v in violations)

    def test_custom_rule(self):
        validator = ProtocolValidator()

        def _custom_rule(trace):
            if not trace.has_step("custom_step"):
                return [ProtocolViolation(
                    protocol_id="PROTO-008",
                    rule_id="CUSTOM-001",
                    message="Missing custom_step",
                )]
            return []

        validator.add_rule("PROTO-008", _custom_rule)
        trace = ProtocolTrace("PROTO-008")
        trace.record("phase1_write")
        trace.record("phase2_commit")
        trace.record("offset_committed")
        violations = validator.validate(trace)
        assert any(v.rule_id == "CUSTOM-001" for v in violations)

    def test_violation_history_accumulates(self):
        validator = ProtocolValidator()
        for _ in range(3):
            trace = ProtocolTrace("PROTO-008")
            trace.record("offset_committed")   # missing phase1_write and phase2_commit
            validator.validate(trace)
        assert validator.total_violations > 0

    def test_supported_protocols_list(self):
        protocols = ProtocolValidator.supported_protocols()
        assert "PROTO-008" in protocols
        assert "PROTO-006" in protocols
        assert "PROTO-019" in protocols

    def test_make_trace_convenience(self):
        trace = ProtocolValidator.make_trace("PROTO-008", "exec-99")
        assert trace.protocol_id == "PROTO-008"
        assert trace.execution_id == "exec-99"
