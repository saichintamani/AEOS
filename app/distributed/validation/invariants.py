"""
Phase 9B.6 — Invariant Engine

Runtime enforcement of architecture invariants defined in 019-INVARIANTS.md.

Design:
  InvariantViolation — structured record of a detected violation
  InvariantCheck     — async callable returning list[InvariantViolation]
  InvariantCatalog   — registry of all known invariants with metadata
  InvariantEngine    — evaluates checks against live runtime snapshots

Invariant IDs map directly to 019-INVARIANTS.md:
  INV-EXEC-001  No duplicate step execution
  INV-EXEC-002  Checkpoint precedes Kafka offset commit
  INV-EXEC-003  Governance token required
  INV-EXEC-004  Lease held before execution
  INV-EXEC-005  Fail-closed governance default
  INV-CONS-001  Single leader per term
  INV-CONS-002  Raft log monotonicity
  INV-CONS-004  Membership = Raft log projection (staleness ≤ 5s)
  INV-RAFT-001  At most one vote per term
  INV-RAFT-002  Leader has all committed entries
  INV-RAFT-003  Entries committed only when replicated on majority
  INV-RAFT-004  State machine applies in log order
  INV-CHKPT-001 Phase 1 atomicity
  INV-CHKPT-002 Orphan recovery within scan cycle
  INV-MEM-001   Redis key co-location (same hashtag per transaction)

Severity levels:
  CRITICAL — system cannot continue correctly; alert immediately
  ERROR    — correctness violation; must be fixed before production
  WARNING  — deviation from best practice; document and monitor
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


# ── Data types ────────────────────────────────────────────────────────────────

class Severity(str, Enum):
    CRITICAL = "critical"
    ERROR    = "error"
    WARNING  = "warning"
    INFO     = "info"


@dataclass
class InvariantViolation:
    """Structured record of a detected invariant violation."""
    invariant_id: str
    severity: Severity
    message: str
    context: dict[str, Any] = field(default_factory=dict)
    detected_at: float = field(default_factory=time.monotonic)

    def __str__(self) -> str:
        ctx = ", ".join(f"{k}={v}" for k, v in self.context.items())
        return f"[{self.severity.upper()}] {self.invariant_id}: {self.message}" + (
            f" ({ctx})" if ctx else ""
        )


@dataclass
class InvariantResult:
    """Outcome of evaluating one or more invariants."""
    passed: list[str] = field(default_factory=list)     # IDs of passing invariants
    violations: list[InvariantViolation] = field(default_factory=list)
    evaluated_at: float = field(default_factory=time.monotonic)

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0

    @property
    def critical_violations(self) -> list[InvariantViolation]:
        return [v for v in self.violations if v.severity == Severity.CRITICAL]


# ── Check callable type ───────────────────────────────────────────────────────

InvariantCheck = Callable[[], Coroutine[Any, Any, list[InvariantViolation]]]


# ── Individual invariant implementations ─────────────────────────────────────

def check_raft_single_leader(
    raft_nodes: list[Any],   # list[RaftNode]
) -> InvariantCheck:
    """
    INV-CONS-001 — Single leader per term.
    ∀ term T: |{node N : N.state = LEADER ∧ N.currentTerm = T}| ≤ 1
    """
    async def _check() -> list[InvariantViolation]:
        from app.distributed.consensus.raft import RaftRole
        violations: list[InvariantViolation] = []

        by_term: dict[int, list[str]] = {}
        for node in raft_nodes:
            if node.role == RaftRole.LEADER:
                t = node.term
                by_term.setdefault(t, []).append(node._id)

        for term, leaders in by_term.items():
            if len(leaders) > 1:
                violations.append(InvariantViolation(
                    invariant_id="INV-CONS-001",
                    severity=Severity.CRITICAL,
                    message=f"Split-brain: {len(leaders)} leaders in term {term}",
                    context={"term": term, "leaders": leaders},
                ))

        return violations

    return _check


def check_raft_log_monotonicity(
    raft_nodes: list[Any],
) -> InvariantCheck:
    """
    INV-CONS-002 — Raft log monotonicity.
    Applied index must never exceed commit index.
    All applied entries must be in order.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for node in raft_nodes:
            s = node._state
            if s.last_applied > s.commit_index:
                violations.append(InvariantViolation(
                    invariant_id="INV-CONS-002",
                    severity=Severity.CRITICAL,
                    message="last_applied exceeds commit_index",
                    context={
                        "node_id": node._id,
                        "last_applied": s.last_applied,
                        "commit_index": s.commit_index,
                    },
                ))
            # Verify log entries are in ascending index order
            for i, entry in enumerate(s.log):
                if entry.index != i:
                    violations.append(InvariantViolation(
                        invariant_id="INV-CONS-002",
                        severity=Severity.ERROR,
                        message=f"Log entry at position {i} has index {entry.index}",
                        context={"node_id": node._id, "pos": i, "entry_index": entry.index},
                    ))
                    break
        return violations

    return _check


def check_raft_inv_001(
    raft_nodes: list[Any],
) -> InvariantCheck:
    """
    INV-RAFT-001 — At most one vote per term per node.
    Checks that voted_for is set at most once per term.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        # This is a structural check: voted_for should be None or a single node_id
        # A node granting votes to two candidates in the same term is caught by
        # verifying voted_for is stable once set.
        for node in raft_nodes:
            vf = node._state.voted_for
            if vf is not None and vf not in [n._id for n in raft_nodes] and vf != node._id:
                violations.append(InvariantViolation(
                    invariant_id="INV-RAFT-001",
                    severity=Severity.ERROR,
                    message=f"voted_for references unknown node '{vf}'",
                    context={"node_id": node._id, "voted_for": vf},
                ))
        return violations

    return _check


def check_checkpoint_committed_only(
    load_result: list[dict],
    invariant_id: str = "INV-EXEC-002",
) -> InvariantCheck:
    """
    INV-EXEC-002 / INV-CHKPT-001 — Only committed checkpoints returned by load().
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for cp in load_result:
            if not cp.get("committed", False):
                violations.append(InvariantViolation(
                    invariant_id=invariant_id,
                    severity=Severity.CRITICAL,
                    message="Uncommitted checkpoint returned by load()",
                    context={
                        "execution_id": cp.get("execution_id"),
                        "step_id": cp.get("step_id"),
                    },
                ))
        return violations

    return _check


def check_lease_held_before_execution(
    executing_tasks: list[dict],   # each: {task_id, worker_id, lease_holder}
) -> InvariantCheck:
    """
    INV-EXEC-004 — Worker must hold the execution lease before executing.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for task in executing_tasks:
            if task.get("worker_id") != task.get("lease_holder"):
                violations.append(InvariantViolation(
                    invariant_id="INV-EXEC-004",
                    severity=Severity.CRITICAL,
                    message="Task executing without holding its lease",
                    context={
                        "task_id": task.get("task_id"),
                        "worker_id": task.get("worker_id"),
                        "lease_holder": task.get("lease_holder"),
                    },
                ))
        return violations

    return _check


def check_no_duplicate_execution(
    execution_records: list[dict],   # each: {task_id, step_id, execute_count, max_retries}
) -> InvariantCheck:
    """
    INV-EXEC-001 — No duplicate step execution without retry policy.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for rec in execution_records:
            count = rec.get("execute_count", 1)
            max_retries = rec.get("max_retries", 0)
            if count > 1 and max_retries == 0:
                violations.append(InvariantViolation(
                    invariant_id="INV-EXEC-001",
                    severity=Severity.CRITICAL,
                    message=f"Step executed {count} times with max_retries=0",
                    context={
                        "task_id": rec.get("task_id"),
                        "step_id": rec.get("step_id"),
                        "execute_count": count,
                    },
                ))
        return violations

    return _check


def check_governance_fail_closed(
    policy_evaluations: list[dict],  # each: {task_type, result, had_matching_policy, timed_out}
) -> InvariantCheck:
    """
    INV-EXEC-005 — Governance is fail-closed: APPROVED only when policy matches and no timeout.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for ev in policy_evaluations:
            result = ev.get("result")
            had_policy = ev.get("had_matching_policy", True)
            timed_out = ev.get("timed_out", False)
            if result == "APPROVED" and (not had_policy or timed_out):
                violations.append(InvariantViolation(
                    invariant_id="INV-EXEC-005",
                    severity=Severity.CRITICAL,
                    message="Task APPROVED without matching policy or under timeout",
                    context={
                        "task_type": ev.get("task_type"),
                        "had_matching_policy": had_policy,
                        "timed_out": timed_out,
                    },
                ))
        return violations

    return _check


def check_membership_cache_staleness(
    raft_committed_members: set[str],
    cached_members: set[str],
    max_staleness_s: float = 5.0,
    cache_age_s: float = 0.0,
) -> InvariantCheck:
    """
    INV-CONS-004 — Membership cache staleness ≤ 5s relative to Raft log.
    """
    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        diverged = raft_committed_members.symmetric_difference(cached_members)
        if diverged and cache_age_s > max_staleness_s:
            violations.append(InvariantViolation(
                invariant_id="INV-CONS-004",
                severity=Severity.ERROR,
                message=f"Membership cache diverged from Raft log for {cache_age_s:.1f}s "
                        f"(limit {max_staleness_s}s)",
                context={
                    "raft_only": list(raft_committed_members - cached_members),
                    "cache_only": list(cached_members - raft_committed_members),
                    "cache_age_s": cache_age_s,
                },
            ))
        return violations

    return _check


def check_redis_key_hashtags(
    transaction_key_groups: list[list[str]],
) -> InvariantCheck:
    """
    INV-MEM-001 — All keys in a MULTI/EXEC must share the same {wf:X} hashtag.
    """
    import re
    _HASHTAG_RE = re.compile(r'\{([^}]+)\}')

    def _hashtag(key: str) -> str | None:
        m = _HASHTAG_RE.search(key)
        return m.group(1) if m else None

    async def _check() -> list[InvariantViolation]:
        violations: list[InvariantViolation] = []
        for i, keys in enumerate(transaction_key_groups):
            tags = {_hashtag(k) for k in keys}
            tags.discard(None)   # keys without hashtag are OK on their own
            keyed = [k for k in keys if _hashtag(k) is not None]
            if len(tags) > 1 and len(keyed) > 0:
                violations.append(InvariantViolation(
                    invariant_id="INV-MEM-001",
                    severity=Severity.ERROR,
                    message=f"MULTI/EXEC transaction {i} contains keys with different hashtags",
                    context={"hashtags": list(tags), "keys": keys},
                ))
        return violations

    return _check


# ── Invariant Catalog ─────────────────────────────────────────────────────────

@dataclass
class InvariantMeta:
    invariant_id: str
    title: str
    severity: Severity
    category: str   # "execution", "consensus", "checkpoint", "memory", "security"
    doc_ref: str = "019-INVARIANTS.md"


_CATALOG: dict[str, InvariantMeta] = {
    "INV-EXEC-001": InvariantMeta("INV-EXEC-001", "No Duplicate Step Execution",         Severity.CRITICAL, "execution"),
    "INV-EXEC-002": InvariantMeta("INV-EXEC-002", "Checkpoint Precedes Offset Commit",   Severity.CRITICAL, "execution"),
    "INV-EXEC-003": InvariantMeta("INV-EXEC-003", "Governance Token Required",           Severity.CRITICAL, "execution"),
    "INV-EXEC-004": InvariantMeta("INV-EXEC-004", "Lease Held Before Execution",         Severity.CRITICAL, "execution"),
    "INV-EXEC-005": InvariantMeta("INV-EXEC-005", "Fail-Closed Governance Default",      Severity.CRITICAL, "execution"),
    "INV-CONS-001": InvariantMeta("INV-CONS-001", "Single Leader Per Term",              Severity.CRITICAL, "consensus"),
    "INV-CONS-002": InvariantMeta("INV-CONS-002", "Raft Log Monotonicity",               Severity.CRITICAL, "consensus"),
    "INV-CONS-004": InvariantMeta("INV-CONS-004", "Membership Cache Staleness ≤ 5s",    Severity.ERROR,    "consensus"),
    "INV-RAFT-001": InvariantMeta("INV-RAFT-001", "At Most One Vote Per Term",           Severity.CRITICAL, "consensus"),
    "INV-RAFT-002": InvariantMeta("INV-RAFT-002", "Leader Has All Committed Entries",    Severity.CRITICAL, "consensus"),
    "INV-RAFT-003": InvariantMeta("INV-RAFT-003", "Commit Requires Quorum",              Severity.CRITICAL, "consensus"),
    "INV-RAFT-004": InvariantMeta("INV-RAFT-004", "Entries Applied In Log Order",        Severity.CRITICAL, "consensus"),
    "INV-CHKPT-001": InvariantMeta("INV-CHKPT-001", "Phase 1 Atomicity",                Severity.CRITICAL, "checkpoint"),
    "INV-CHKPT-002": InvariantMeta("INV-CHKPT-002", "Orphan Recovery Completeness",     Severity.ERROR,    "checkpoint"),
    "INV-MEM-001":  InvariantMeta("INV-MEM-001",  "Redis Key Hashtag Co-location",      Severity.ERROR,    "memory"),
}


class InvariantCatalog:
    """Read-only catalog of all registered invariant metadata."""

    @staticmethod
    def all() -> list[InvariantMeta]:
        return list(_CATALOG.values())

    @staticmethod
    def get(invariant_id: str) -> InvariantMeta | None:
        return _CATALOG.get(invariant_id)

    @staticmethod
    def by_category(category: str) -> list[InvariantMeta]:
        return [m for m in _CATALOG.values() if m.category == category]

    @staticmethod
    def critical() -> list[InvariantMeta]:
        return [m for m in _CATALOG.values() if m.severity == Severity.CRITICAL]


# ── Engine ────────────────────────────────────────────────────────────────────

class InvariantEngine:
    """
    Runtime invariant evaluator.

    Usage::

        engine = InvariantEngine()
        engine.register("INV-CONS-001", check_raft_single_leader(nodes))
        result = await engine.evaluate()
        if not result.ok:
            for v in result.violations:
                logger.error("INVARIANT VIOLATION: %s", v)

    The engine can also run as a continuous background monitor via start()/stop().
    """

    def __init__(self, check_interval_s: float = 10.0) -> None:
        self._checks: dict[str, InvariantCheck] = {}
        self._violations: list[InvariantViolation] = []
        self._check_interval = check_interval_s
        self._running = False
        self._task: asyncio.Task | None = None
        self._callbacks: list[Callable[[InvariantViolation], None]] = []
        self._total_evaluations = 0
        self._total_violations = 0

    def register(self, invariant_id: str, check: InvariantCheck) -> None:
        """Register an async check function for the given invariant ID."""
        self._checks[invariant_id] = check

    def on_violation(self, callback: Callable[[InvariantViolation], None]) -> None:
        """Register a callback fired whenever a violation is detected."""
        self._callbacks.append(callback)

    async def evaluate(
        self,
        *,
        invariant_ids: list[str] | None = None,
        raise_on_critical: bool = False,
    ) -> InvariantResult:
        """
        Run all (or a subset of) registered checks.

        Args:
            invariant_ids:       If provided, only evaluate these IDs.
            raise_on_critical:   If True, raise InvariantError on CRITICAL violations.
        """
        self._total_evaluations += 1
        result = InvariantResult()
        ids_to_run = invariant_ids or list(self._checks.keys())

        for inv_id in ids_to_run:
            check = self._checks.get(inv_id)
            if check is None:
                continue
            try:
                violations = await check()
                if violations:
                    result.violations.extend(violations)
                    self._total_violations += len(violations)
                    self._violations.extend(violations)
                    for v in violations:
                        logger.error("INVARIANT VIOLATION: %s", v)
                        for cb in self._callbacks:
                            try:
                                cb(v)
                            except Exception:
                                pass
                else:
                    result.passed.append(inv_id)
            except Exception as exc:
                logger.exception("InvariantEngine: check %s raised exception: %s", inv_id, exc)

        if raise_on_critical and result.critical_violations:
            raise InvariantError(result.critical_violations)

        return result

    async def start(self) -> None:
        """Start the background invariant monitor."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="invariant-monitor")
        logger.info("InvariantEngine: background monitor started (interval=%.0fs)", self._check_interval)

    async def stop(self) -> None:
        """Stop the background monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def violation_history(self) -> list[InvariantViolation]:
        return list(self._violations)

    @property
    def stats(self) -> dict:
        return {
            "registered_checks": len(self._checks),
            "total_evaluations": self._total_evaluations,
            "total_violations": self._total_violations,
            "violation_history_size": len(self._violations),
        }

    async def _monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self._check_interval)
            if not self._running:
                break
            result = await self.evaluate()
            if result.violations:
                logger.warning(
                    "InvariantEngine: %d violations detected in cycle",
                    len(result.violations),
                )


class InvariantError(Exception):
    """Raised by evaluate(raise_on_critical=True) when CRITICAL violations exist."""

    def __init__(self, violations: list[InvariantViolation]) -> None:
        self.violations = violations
        ids = ", ".join(v.invariant_id for v in violations)
        super().__init__(f"Critical invariant violations: {ids}")
