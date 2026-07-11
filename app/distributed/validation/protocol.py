"""
Phase 9B.6 — Protocol Validator

Validates that message exchanges conform to the sequence diagrams and
preconditions defined in 016-PROTOCOL_SPECIFICATION.md.

Design:
  ProtocolStep    — a single message in a protocol exchange
  ProtocolTrace   — ordered sequence of steps for one protocol execution
  ProtocolRule    — a named validation rule applied to a trace
  ProtocolValidator — registry + evaluator

Protocols validated:
  PROTO-006  Task Dispatch
  PROTO-008  Two-Phase Checkpoint
  PROTO-009  Checkpoint Recovery
  PROTO-019  Execution Lease Acquisition

Approach:
  1. Test code or runtime code records ProtocolSteps into a ProtocolTrace.
  2. ProtocolValidator.validate(trace) applies all rules for that protocol.
  3. Violations are returned as ProtocolViolation objects.

This is not a network proxy — it operates on recorded message traces,
suitable for both unit tests and runtime audit logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class ProtocolStep:
    """A single message or action in a protocol exchange."""
    step_name: str           # e.g. "lease_acquired", "phase1_written"
    actor: str               # e.g. "worker", "coordinator", "redis"
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)
    success: bool = True


@dataclass
class ProtocolTrace:
    """Ordered sequence of steps for a single protocol execution."""
    protocol_id: str         # e.g. "PROTO-008"
    execution_id: str = ""
    steps: list[ProtocolStep] = field(default_factory=list)

    def record(
        self,
        step_name: str,
        actor: str = "",
        *,
        payload: dict | None = None,
        success: bool = True,
    ) -> None:
        self.steps.append(ProtocolStep(
            step_name=step_name,
            actor=actor,
            payload=payload or {},
            success=success,
        ))

    def step_names(self) -> list[str]:
        return [s.step_name for s in self.steps]

    def has_step(self, step_name: str) -> bool:
        return step_name in self.step_names()

    def find(self, step_name: str) -> ProtocolStep | None:
        for s in self.steps:
            if s.step_name == step_name:
                return s
        return None

    def successful_steps(self) -> list[ProtocolStep]:
        return [s for s in self.steps if s.success]


@dataclass
class ProtocolViolation:
    protocol_id: str
    rule_id: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    detected_at: float = field(default_factory=time.monotonic)

    def __str__(self) -> str:
        return f"[{self.protocol_id}][{self.rule_id}] {self.message}"


ProtocolRule = Callable[[ProtocolTrace], list[ProtocolViolation]]


# ── Built-in rules ────────────────────────────────────────────────────────────

def _require_step(protocol_id: str, rule_id: str, step_name: str) -> ProtocolRule:
    """Assert that the given step is present in the trace."""
    def _rule(trace: ProtocolTrace) -> list[ProtocolViolation]:
        if not trace.has_step(step_name):
            return [ProtocolViolation(
                protocol_id=protocol_id,
                rule_id=rule_id,
                message=f"Required step '{step_name}' not found in trace",
                context={"steps_present": trace.step_names()},
            )]
        return []
    return _rule


def _require_order(
    protocol_id: str, rule_id: str, before: str, after: str
) -> ProtocolRule:
    """Assert that `before` step appears strictly before `after` step."""
    def _rule(trace: ProtocolTrace) -> list[ProtocolViolation]:
        names = trace.step_names()
        if before not in names or after not in names:
            return []  # missing steps handled by _require_step rules
        if names.index(before) >= names.index(after):
            return [ProtocolViolation(
                protocol_id=protocol_id,
                rule_id=rule_id,
                message=f"Step '{before}' must precede '{after}' but did not",
                context={"order": names},
            )]
        return []
    return _rule


def _require_success(protocol_id: str, rule_id: str, step_name: str) -> ProtocolRule:
    """Assert that the given step completed successfully."""
    def _rule(trace: ProtocolTrace) -> list[ProtocolViolation]:
        step = trace.find(step_name)
        if step and not step.success:
            return [ProtocolViolation(
                protocol_id=protocol_id,
                rule_id=rule_id,
                message=f"Step '{step_name}' failed but must succeed",
                context={"payload": step.payload},
            )]
        return []
    return _rule


def _no_step_after_terminal(
    protocol_id: str, rule_id: str, terminal_step: str, forbidden_steps: list[str]
) -> ProtocolRule:
    """Assert that no `forbidden_steps` appear after `terminal_step`."""
    def _rule(trace: ProtocolTrace) -> list[ProtocolViolation]:
        names = trace.step_names()
        if terminal_step not in names:
            return []
        terminal_pos = names.index(terminal_step)
        after = names[terminal_pos + 1:]
        violations = []
        for step in forbidden_steps:
            if step in after:
                violations.append(ProtocolViolation(
                    protocol_id=protocol_id,
                    rule_id=rule_id,
                    message=f"Step '{step}' appeared after terminal step '{terminal_step}'",
                    context={"terminal_pos": terminal_pos, "step_order": names},
                ))
        return violations
    return _rule


def _mutual_exclusion(
    protocol_id: str, rule_id: str, steps: list[str], message: str
) -> ProtocolRule:
    """Assert that at most one of `steps` appears in the trace."""
    def _rule(trace: ProtocolTrace) -> list[ProtocolViolation]:
        found = [s for s in steps if trace.has_step(s)]
        if len(found) > 1:
            return [ProtocolViolation(
                protocol_id=protocol_id,
                rule_id=rule_id,
                message=message,
                context={"found_steps": found},
            )]
        return []
    return _rule


# ── Protocol rule sets ────────────────────────────────────────────────────────

_PROTO_006_RULES: list[ProtocolRule] = [
    # PROTO-006: Task Dispatch
    # Sequence: lease_acquired → task_accepted_event → execution_started →
    #           phase1_checkpoint → phase2_checkpoint → task_completed_event → offset_committed
    _require_step("PROTO-006", "P006-R001", "lease_acquired"),
    _require_step("PROTO-006", "P006-R002", "task_accepted_event"),
    _require_step("PROTO-006", "P006-R003", "execution_started"),
    _require_order("PROTO-006", "P006-R004", "lease_acquired", "execution_started"),
    _require_order("PROTO-006", "P006-R005", "execution_started", "phase1_checkpoint"),
    _require_order("PROTO-006", "P006-R006", "phase2_checkpoint", "offset_committed"),
    _require_success("PROTO-006", "P006-R007", "lease_acquired"),
]

_PROTO_008_RULES: list[ProtocolRule] = [
    # PROTO-008: Two-Phase Checkpoint
    # Phase 1: write(committed=False) → Phase 2: commit → offset_committed
    _require_step("PROTO-008", "P008-R001", "phase1_write"),
    _require_step("PROTO-008", "P008-R002", "phase2_commit"),
    _require_order("PROTO-008", "P008-R003", "phase1_write", "phase2_commit"),
    _require_order("PROTO-008", "P008-R004", "phase2_commit", "offset_committed"),
    _require_success("PROTO-008", "P008-R005", "phase1_write"),
    _require_success("PROTO-008", "P008-R006", "phase2_commit"),
    # Invariant: offset must not be committed before phase 2
    _no_step_after_terminal("PROTO-008", "P008-R007", "offset_committed", ["phase1_write", "phase2_commit"]),
]

_PROTO_009_RULES: list[ProtocolRule] = [
    # PROTO-009: Checkpoint Recovery
    # orphan_detected → checkpoint_loaded → task_requeued (or task_resumed)
    _require_step("PROTO-009", "P009-R001", "orphan_detected"),
    _require_step("PROTO-009", "P009-R002", "checkpoint_loaded"),
    _require_order("PROTO-009", "P009-R003", "orphan_detected", "checkpoint_loaded"),
    # Exactly one of task_requeued or task_resumed (not both, not neither)
    _mutual_exclusion("PROTO-009", "P009-R004",
                      ["task_requeued", "task_resumed"],
                      "Both task_requeued and task_resumed present — pick one"),
]

_PROTO_019_RULES: list[ProtocolRule] = [
    # PROTO-019: Execution Lease Acquisition
    # setnx_attempt → setnx_success → fencing_token_incremented
    # OR: setnx_attempt → setnx_failed (stop — do not execute)
    _require_step("PROTO-019", "P019-R001", "setnx_attempt"),
    _require_order("PROTO-019", "P019-R002", "setnx_attempt", "setnx_result"),
    # If setnx succeeded, fencing token must be incremented before execution
    lambda trace: (
        [ProtocolViolation(
            protocol_id="PROTO-019",
            rule_id="P019-R003",
            message="Execution started without fencing_token_incremented after setnx_success",
            context={},
        )]
        if (trace.has_step("setnx_success") and
            trace.has_step("execution_started") and
            not trace.has_step("fencing_token_incremented"))
        else []
    ),
    # If setnx failed, execution must not start
    _no_step_after_terminal("PROTO-019", "P019-R004", "setnx_failed", ["execution_started"]),
]

_PROTO_001_RULES: list[ProtocolRule] = [
    # PROTO-001: Node Join
    # join_request → raft_log_append → raft_quorum_ack → capability_registered → join_response
    _require_step("PROTO-001", "P001-R001", "join_request"),
    _require_step("PROTO-001", "P001-R002", "raft_log_append"),
    _require_step("PROTO-001", "P001-R003", "capability_registered"),
    _require_step("PROTO-001", "P001-R004", "join_response"),
    _require_order("PROTO-001", "P001-R005", "join_request", "raft_log_append"),
    _require_order("PROTO-001", "P001-R006", "raft_log_append", "capability_registered"),
    _require_order("PROTO-001", "P001-R007", "capability_registered", "join_response"),
]


# ── Registry ──────────────────────────────────────────────────────────────────

_RULE_REGISTRY: dict[str, list[ProtocolRule]] = {
    "PROTO-001": _PROTO_001_RULES,
    "PROTO-006": _PROTO_006_RULES,
    "PROTO-008": _PROTO_008_RULES,
    "PROTO-009": _PROTO_009_RULES,
    "PROTO-019": _PROTO_019_RULES,
}


# ── Validator ─────────────────────────────────────────────────────────────────

class ProtocolValidator:
    """
    Validates ProtocolTraces against the rules defined for each protocol.

    Usage::

        validator = ProtocolValidator()

        # Record a two-phase checkpoint exchange
        trace = ProtocolTrace(protocol_id="PROTO-008", execution_id="exec-1")
        trace.record("phase1_write", "worker")
        trace.record("phase2_commit", "worker")
        trace.record("offset_committed", "worker")

        violations = validator.validate(trace)
        assert len(violations) == 0
    """

    def __init__(self) -> None:
        self._custom_rules: dict[str, list[ProtocolRule]] = {}
        self._all_violations: list[ProtocolViolation] = []

    def add_rule(self, protocol_id: str, rule: ProtocolRule) -> None:
        """Add a custom rule for a protocol."""
        self._custom_rules.setdefault(protocol_id, []).append(rule)

    def validate(self, trace: ProtocolTrace) -> list[ProtocolViolation]:
        """Validate a protocol trace and return any violations found."""
        rules = (
            _RULE_REGISTRY.get(trace.protocol_id, [])
            + self._custom_rules.get(trace.protocol_id, [])
        )
        violations: list[ProtocolViolation] = []
        for rule in rules:
            try:
                violations.extend(rule(trace))
            except Exception as exc:
                violations.append(ProtocolViolation(
                    protocol_id=trace.protocol_id,
                    rule_id="INTERNAL",
                    message=f"Rule evaluation error: {exc}",
                ))
        self._all_violations.extend(violations)
        return violations

    def validate_all(self, traces: list[ProtocolTrace]) -> list[ProtocolViolation]:
        """Validate multiple traces and return all violations."""
        all_violations: list[ProtocolViolation] = []
        for trace in traces:
            all_violations.extend(self.validate(trace))
        return all_violations

    @property
    def violation_history(self) -> list[ProtocolViolation]:
        return list(self._all_violations)

    @property
    def total_violations(self) -> int:
        return len(self._all_violations)

    @staticmethod
    def supported_protocols() -> list[str]:
        return list(_RULE_REGISTRY.keys())

    @staticmethod
    def make_trace(protocol_id: str, execution_id: str = "") -> ProtocolTrace:
        """Convenience factory for building traces."""
        return ProtocolTrace(protocol_id=protocol_id, execution_id=execution_id)
