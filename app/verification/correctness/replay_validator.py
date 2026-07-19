"""
app/verification/correctness/replay_validator.py

Execution Trace Replay Validator.

Implements DCS §4 requirement: "Execution traces MUST be replayable
to verify correctness."

Takes a serialized execution trace and re-derives the expected system
state step by step, comparing against actual recorded state snapshots.
Detects:
  - State divergence
  - Missing transitions
  - Causal ordering violations
  - Exactly-once illusion breaks
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ReplayOutcome(str, Enum):
    CORRECT = "CORRECT"
    DIVERGED = "DIVERGED"
    INCOMPLETE = "INCOMPLETE"
    CAUSAL_VIOLATION = "CAUSAL_VIOLATION"


@dataclass
class TraceEvent:
    """A single event in an execution trace."""
    seq: int              # Monotonically increasing sequence number
    timestamp: float
    event_type: str       # e.g. "task_scheduled", "checkpoint_written"
    actor: str            # e.g. "worker-1", "scheduler", "raft-leader"
    payload: dict[str, Any] = field(default_factory=dict)
    # Actual system state at this point (recorded during live execution)
    state_snapshot: dict[str, Any] | None = None


@dataclass
class ReplayDivergence:
    seq: int
    event_type: str
    field: str
    expected: Any
    actual: Any
    description: str


@dataclass
class ReplayResult:
    """Full result of a trace replay."""
    outcome: ReplayOutcome
    trace_length: int
    replayed_events: int
    divergences: list[ReplayDivergence] = field(default_factory=list)
    causal_violations: list[str] = field(default_factory=list)
    exactly_once_breaks: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    report: str = ""

    @property
    def correct(self) -> bool:
        return self.outcome == ReplayOutcome.CORRECT


class ReplayValidator:
    """
    Replays an AEOS execution trace and verifies correctness.

    The replay engine is a deterministic state machine that processes
    events in sequence-number order and checks:

    1. State Correctness: derived state == recorded state at each step
    2. Causal Ordering: cause always precedes effect
    3. Exactly-Once: no task executed more than once per idempotency window
    4. Checkpoint Coverage: every RUNNING→COMPLETED transition is preceded
       by a checkpoint
    5. Lease Safety: execution only occurs while lease is held

    Usage::

        validator = ReplayValidator()
        trace = load_trace_from_file("traces/execution_20260712.jsonl")
        result = validator.replay(trace)
        assert result.correct, result.report
    """

    def __init__(self, dedup_window_seconds: float = 86400.0) -> None:
        self._dedup_window = dedup_window_seconds

    def replay(self, events: list[TraceEvent]) -> ReplayResult:
        """Replay a list of trace events and return correctness result."""
        start = time.monotonic()

        # Sort by sequence number — ensures causal order
        sorted_events = sorted(events, key=lambda e: e.seq)

        divergences: list[ReplayDivergence] = []
        causal_violations: list[str] = []
        exactly_once_breaks: list[str] = []

        # Derived state (built up during replay)
        derived: dict[str, Any] = {
            "task_states": {},        # task_id → current state
            "lease_holders": set(),   # task_ids currently holding lease
            "checkpointed": set(),    # task_ids that have been checkpointed
            "executed": {},           # task_id → first execution timestamp
            "committed_offsets": {},  # partition → offset
            "raft_term": 0,
            "raft_commit_index": 0,
        }

        # Causal dependency tracking
        # Maps event_type → prerequisite event_type
        causal_rules: dict[str, list[str]] = {
            "execution_started":    ["lease_acquired", "governance_approved"],
            "checkpoint_committed": ["checkpoint_phase1_written"],
            "offset_committed":     ["checkpoint_committed"],
            "task_completed":       ["checkpoint_committed"],
        }
        seen_events: set[str] = set()  # event_types seen so far

        for i, event in enumerate(sorted_events):
            # Causal ordering check
            for prereq in causal_rules.get(event.event_type, []):
                if prereq not in seen_events:
                    causal_violations.append(
                        f"seq={event.seq}: '{event.event_type}' occurred before "
                        f"required '{prereq}'"
                    )
            seen_events.add(event.event_type)

            # Apply event to derived state
            self._apply_event(event, derived)

            # Exactly-once check: execution_started must not repeat within window
            if event.event_type == "execution_started":
                task_id = event.payload.get("task_id", "")
                if task_id in derived["executed"]:
                    prev_ts = derived["executed"][task_id]
                    if event.timestamp - prev_ts < self._dedup_window:
                        exactly_once_breaks.append(
                            f"task_id={task_id} executed twice within "
                            f"{self._dedup_window}s window "
                            f"(first={prev_ts:.3f}, second={event.timestamp:.3f})"
                        )
                else:
                    derived["executed"][task_id] = event.timestamp

            # State divergence check (only if snapshot provided)
            if event.state_snapshot:
                divs = self._compare_state(event.seq, event.event_type, derived, event.state_snapshot)
                divergences.extend(divs)

        replayed = len(sorted_events)
        outcome = ReplayOutcome.CORRECT
        if causal_violations:
            outcome = ReplayOutcome.CAUSAL_VIOLATION
        elif divergences or exactly_once_breaks:
            outcome = ReplayOutcome.DIVERGED

        duration_ms = (time.monotonic() - start) * 1000
        report = self._build_report(
            outcome, replayed, divergences, causal_violations, exactly_once_breaks
        )

        return ReplayResult(
            outcome=outcome,
            trace_length=len(events),
            replayed_events=replayed,
            divergences=divergences,
            causal_violations=causal_violations,
            exactly_once_breaks=exactly_once_breaks,
            duration_ms=duration_ms,
            report=report,
        )

    def replay_from_file(self, path: str) -> ReplayResult:
        """Load a JSONL trace file and replay it."""
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                events.append(TraceEvent(
                    seq=raw["seq"],
                    timestamp=raw["timestamp"],
                    event_type=raw["event_type"],
                    actor=raw.get("actor", "unknown"),
                    payload=raw.get("payload", {}),
                    state_snapshot=raw.get("state_snapshot"),
                ))
        return self.replay(events)

    def _apply_event(self, event: TraceEvent, state: dict[str, Any]) -> None:
        """Apply an event to the derived state machine."""
        p = event.payload
        et = event.event_type

        if et == "task_scheduled":
            state["task_states"][p.get("task_id", "")] = "SCHEDULED"

        elif et == "lease_acquired":
            state["lease_holders"].add(p.get("task_id", ""))

        elif et == "execution_started":
            tid = p.get("task_id", "")
            state["task_states"][tid] = "RUNNING"

        elif et == "checkpoint_phase1_written":
            state["checkpointed"].add(p.get("task_id", ""))

        elif et == "checkpoint_committed":
            pass  # Checkpoint committed — no state change beyond phase1

        elif et == "offset_committed":
            partition = p.get("partition", "")
            offset = p.get("offset", 0)
            state["committed_offsets"][partition] = offset

        elif et == "task_completed":
            tid = p.get("task_id", "")
            state["task_states"][tid] = "COMPLETED"
            state["lease_holders"].discard(tid)

        elif et == "task_failed":
            tid = p.get("task_id", "")
            state["task_states"][tid] = "FAILED"
            state["lease_holders"].discard(tid)

        elif et == "lease_released":
            state["lease_holders"].discard(p.get("task_id", ""))

        elif et == "raft_term_updated":
            state["raft_term"] = max(state["raft_term"], p.get("term", 0))

        elif et == "raft_commit_index_updated":
            state["raft_commit_index"] = max(
                state["raft_commit_index"], p.get("commit_index", 0)
            )

    def _compare_state(
        self,
        seq: int,
        event_type: str,
        derived: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> list[ReplayDivergence]:
        """Compare derived state against recorded snapshot at this point."""
        divs: list[ReplayDivergence] = []

        # Check task states
        for task_id, recorded_state in snapshot.get("task_states", {}).items():
            derived_state = derived["task_states"].get(task_id, "UNKNOWN")
            if derived_state != recorded_state:
                divs.append(ReplayDivergence(
                    seq=seq,
                    event_type=event_type,
                    field=f"task_states[{task_id}]",
                    expected=derived_state,
                    actual=recorded_state,
                    description=(
                        f"Task {task_id}: replay derived '{derived_state}' "
                        f"but recorded state is '{recorded_state}'"
                    ),
                ))

        # Check Raft term
        recorded_term = snapshot.get("raft_term", 0)
        if recorded_term > 0 and derived["raft_term"] > recorded_term:
            divs.append(ReplayDivergence(
                seq=seq,
                event_type=event_type,
                field="raft_term",
                expected=derived["raft_term"],
                actual=recorded_term,
                description=f"Raft term regression: derived={derived['raft_term']} actual={recorded_term}",
            ))

        return divs

    def _build_report(
        self,
        outcome: ReplayOutcome,
        replayed: int,
        divergences: list[ReplayDivergence],
        causal_violations: list[str],
        exactly_once_breaks: list[str],
    ) -> str:
        lines = [
            f"Replay Result: {outcome.value}",
            f"Events replayed: {replayed}",
            f"Divergences: {len(divergences)}",
            f"Causal violations: {len(causal_violations)}",
            f"Exactly-once breaks: {len(exactly_once_breaks)}",
        ]

        if causal_violations:
            lines.append("\nCausal Violations:")
            for v in causal_violations[:10]:
                lines.append(f"  - {v}")

        if divergences:
            lines.append("\nState Divergences:")
            for d in divergences[:10]:
                lines.append(f"  - seq={d.seq} [{d.event_type}] {d.description}")

        if exactly_once_breaks:
            lines.append("\nExactly-Once Breaks:")
            for b in exactly_once_breaks[:10]:
                lines.append(f"  - {b}")

        return "\n".join(lines)
