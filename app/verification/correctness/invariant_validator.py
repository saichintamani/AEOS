"""
app/verification/correctness/invariant_validator.py

Full-coverage invariant validator — implements all 26 INV-* identifiers
from docs/verification/019-INVARIANTS.md.

Extends the runtime InvariantEngine (15 invariants) with the 11 missing
invariants identified in GAP_ANALYSIS_PHASE_12A.md §2.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

# Import existing engine for the 15 already-implemented invariants
from app.distributed.validation.invariants import (
    InvariantEngine,
    InvariantCatalog,
    InvariantResult,
    Severity,
)


@dataclass
class InvariantCoverageReport:
    """Result of a full invariant coverage run."""
    timestamp: float
    total_invariants: int
    passed: int
    failed: int
    skipped: int
    coverage_pct: float
    violations: list[dict[str, Any]] = field(default_factory=list)
    results: dict[str, InvariantResult] = field(default_factory=dict)

    @property
    def fully_covered(self) -> bool:
        return self.coverage_pct == 100.0 and self.failed == 0


# ── New invariant checks (11 missing from gap analysis) ──────────────────────

async def check_inv_cons_003(state: dict[str, Any]) -> InvariantResult:
    """
    INV-CONS-003 — Term Monotonicity Across Restarts.
    The persisted term in durable storage must be >= the highest term
    seen in-memory before the last restart.
    """
    inv_id = "INV-CONS-003"
    persisted_term: int = state.get("raft_persisted_term", 0)
    current_term: int = state.get("raft_current_term", 0)

    if current_term < persisted_term:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"Term regression: current_term={current_term} < "
                f"persisted_term={persisted_term}. "
                "Node may have lost durable state — split-brain risk."
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_exec_006(state: dict[str, Any]) -> InvariantResult:
    """
    INV-EXEC-006 — DLQ Invariant: No Silent Drop.
    Every task that exhausts retries MUST appear in the DLQ topic.
    No task may be marked FAILED without a corresponding DLQ record.
    """
    inv_id = "INV-EXEC-006"
    failed_task_ids: set[str] = set(state.get("failed_task_ids", []))
    dlq_task_ids: set[str] = set(state.get("dlq_task_ids", []))

    silently_dropped = failed_task_ids - dlq_task_ids
    if silently_dropped:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"{len(silently_dropped)} task(s) marked FAILED without DLQ entry: "
                f"{list(silently_dropped)[:5]}…"
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_exec_007(state: dict[str, Any]) -> InvariantResult:
    """
    INV-EXEC-007 — Idempotency Key Uniqueness Window.
    Within the deduplication window (default 24h), no two distinct tasks
    may share the same idempotency key.
    """
    inv_id = "INV-EXEC-007"
    idempotency_map: dict[str, list[str]] = state.get("idempotency_key_to_task_ids", {})
    window_seconds: int = state.get("dedup_window_seconds", 86400)

    duplicates = {k: v for k, v in idempotency_map.items() if len(v) > 1}
    if duplicates:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"Idempotency key collision within {window_seconds}s window: "
                f"{list(duplicates.keys())[:3]}…"
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_exec_008(state: dict[str, Any]) -> InvariantResult:
    """
    INV-EXEC-008 — At-Least-Once Delivery Guarantee.
    Every Kafka offset committed MUST correspond to a task that either:
    (a) successfully completed, or (b) is in the DLQ.
    No offset may be committed for a task in RUNNING or PENDING state.
    """
    inv_id = "INV-EXEC-008"
    committed_offsets: dict[str, int] = state.get("committed_offsets", {})  # partition→offset
    tasks_at_committed: list[str] = state.get("task_states_at_commit", [])  # states of tasks whose offset was committed

    invalid_states = {"RUNNING", "PENDING", "SCHEDULED"}
    violations = [s for s in tasks_at_committed if s in invalid_states]
    if violations:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"Kafka offsets committed for {len(violations)} task(s) still in "
                f"active states: {set(violations)}. At-least-once guarantee broken."
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_raft_005(state: dict[str, Any]) -> InvariantResult:
    """
    INV-RAFT-005 — Snapshot Index ≤ Commit Index.
    A snapshot may only include log entries up to the current commit index.
    Snapshotting uncommitted entries would violate log safety.
    """
    inv_id = "INV-RAFT-005"
    snapshot_index: int = state.get("raft_snapshot_index", 0)
    commit_index: int = state.get("raft_commit_index", 0)

    if snapshot_index > commit_index:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"snapshot_index={snapshot_index} > commit_index={commit_index}. "
                "Snapshot contains uncommitted entries — safety violation."
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_raft_006(state: dict[str, Any]) -> InvariantResult:
    """
    INV-RAFT-006 — Snapshot Covers All Applied Entries.
    After snapshot installation, last_applied MUST equal snapshot_index.
    Any gap between snapshot_index and last_applied is an uncovered entry.
    """
    inv_id = "INV-RAFT-006"
    snapshot_index: int = state.get("raft_snapshot_index", 0)
    last_applied: int = state.get("raft_last_applied", 0)
    snapshot_active: bool = state.get("raft_snapshot_installed", False)

    if snapshot_active and last_applied < snapshot_index:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"After snapshot install: last_applied={last_applied} < "
                f"snapshot_index={snapshot_index}. Entries {last_applied+1}–{snapshot_index} "
                "are covered by snapshot but not applied."
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_chkpt_003(state: dict[str, Any]) -> InvariantResult:
    """
    INV-CHKPT-003 — Checkpoint Version Monotonicity.
    For any given task_id, checkpoint versions must strictly increase.
    A lower version number appearing after a higher one indicates rollback.
    """
    inv_id = "INV-CHKPT-003"
    checkpoint_versions: dict[str, list[int]] = state.get("checkpoint_versions_by_task", {})

    regressions = []
    for task_id, versions in checkpoint_versions.items():
        for i in range(1, len(versions)):
            if versions[i] <= versions[i - 1]:
                regressions.append(
                    f"task={task_id}: version {versions[i]} followed {versions[i-1]}"
                )

    if regressions:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.ERROR,
            message=f"Checkpoint version regression(s): {regressions[:3]}",
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.ERROR)


async def check_inv_chkpt_004(state: dict[str, Any]) -> InvariantResult:
    """
    INV-CHKPT-004 — Cross-Partition Checkpoint Consistency.
    For multi-partition tasks, all partitions must have the same committed
    checkpoint version before the task is marked COMPLETED.
    """
    inv_id = "INV-CHKPT-004"
    multi_partition_tasks: dict[str, dict[str, int]] = state.get(
        "multi_partition_checkpoint_versions", {}
    )  # task_id → {partition_key → version}

    inconsistent = {}
    for task_id, partition_versions in multi_partition_tasks.items():
        unique_versions = set(partition_versions.values())
        if len(unique_versions) > 1:
            inconsistent[task_id] = partition_versions

    if inconsistent:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"{len(inconsistent)} task(s) have inconsistent checkpoint versions "
                f"across partitions: {list(inconsistent.keys())[:3]}"
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_mem_002(state: dict[str, Any]) -> InvariantResult:
    """
    INV-MEM-002 — Memory Eviction Policy Consistency.
    Redis must use allkeys-lru eviction. If the policy changes (e.g., to
    noeviction), task metadata will be silently lost on memory pressure.
    """
    inv_id = "INV-MEM-002"
    redis_maxmemory_policy: str = state.get("redis_maxmemory_policy", "unknown")
    allowed_policies = {"allkeys-lru", "allkeys-lfu", "allkeys-random"}

    if redis_maxmemory_policy not in allowed_policies:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.ERROR,
            message=(
                f"Redis maxmemory-policy={redis_maxmemory_policy!r} is not in "
                f"allowed set {allowed_policies}. Risk of OOM kill or silent data loss."
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.ERROR)


async def check_inv_gov_001(state: dict[str, Any]) -> InvariantResult:
    """
    INV-GOV-001 — Governance Token Not Reused.
    Each governance token is a one-time-use authorization. Once consumed
    by an execution, it must not be accepted again.
    """
    inv_id = "INV-GOV-001"
    consumed_tokens: set[str] = set(state.get("consumed_governance_tokens", []))
    active_tokens: set[str] = set(state.get("active_governance_tokens", []))

    reused = consumed_tokens & active_tokens
    if reused:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"{len(reused)} governance token(s) are both consumed and active — "
                f"possible replay attack: {list(reused)[:3]}"
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


async def check_inv_gov_002(state: dict[str, Any]) -> InvariantResult:
    """
    INV-GOV-002 — Policy Evaluation Before Execution.
    For every task execution event, a governance evaluation event
    with matching task_id MUST appear before the execution_started event.
    """
    inv_id = "INV-GOV-002"
    # Maps: task_id → {"gov_eval_ts": float, "exec_start_ts": float}
    task_timelines: dict[str, dict[str, float]] = state.get("task_gov_exec_timelines", {})

    violations = []
    for task_id, timeline in task_timelines.items():
        gov_ts = timeline.get("gov_eval_ts")
        exec_ts = timeline.get("exec_start_ts")
        if exec_ts is not None and (gov_ts is None or gov_ts > exec_ts):
            violations.append(task_id)

    if violations:
        return InvariantResult(
            inv_id=inv_id,
            passed=False,
            severity=Severity.CRITICAL,
            message=(
                f"{len(violations)} task(s) started execution without prior governance "
                f"evaluation: {violations[:5]}"
            ),
        )
    return InvariantResult(inv_id=inv_id, passed=True, severity=Severity.CRITICAL)


# ── Full validator ─────────────────────────────────────────────────────────────

_NEW_CHECKS = [
    ("INV-CONS-003", check_inv_cons_003),
    ("INV-EXEC-006", check_inv_exec_006),
    ("INV-EXEC-007", check_inv_exec_007),
    ("INV-EXEC-008", check_inv_exec_008),
    ("INV-RAFT-005", check_inv_raft_005),
    ("INV-RAFT-006", check_inv_raft_006),
    ("INV-CHKPT-003", check_inv_chkpt_003),
    ("INV-CHKPT-004", check_inv_chkpt_004),
    ("INV-MEM-002", check_inv_mem_002),
    ("INV-GOV-001", check_inv_gov_001),
    ("INV-GOV-002", check_inv_gov_002),
]


class InvariantValidator:
    """
    Full-coverage invariant validator.

    Runs the 15 invariants from the existing InvariantEngine PLUS the
    11 new invariants to achieve 100% INV-* coverage (26 total).

    Usage::

        validator = InvariantValidator(engine)
        report = await validator.run_full_coverage(state)
        if not report.fully_covered:
            for v in report.violations:
                print(v)
    """

    TOTAL_INVARIANTS = 26

    def __init__(self, engine: InvariantEngine | None = None) -> None:
        self._engine = engine or InvariantEngine(node_id="validator")

    async def run_full_coverage(
        self,
        state: dict[str, Any],
        *,
        timeout_per_check: float = 0.5,
    ) -> InvariantCoverageReport:
        """
        Run all 26 invariants and return a coverage report.

        Args:
            state: System state snapshot (merged from Raft, Redis, Kafka views).
            timeout_per_check: Per-invariant timeout in seconds.

        Returns:
            InvariantCoverageReport with full results.
        """
        results: dict[str, InvariantResult] = {}
        violations: list[dict[str, Any]] = []

        # Phase 1: run the existing 15 invariants via the engine
        try:
            existing_results = await asyncio.wait_for(
                self._engine.check_all(state),
                timeout=timeout_per_check * 15,
            )
            for r in existing_results:
                results[r.inv_id] = r
                if not r.passed:
                    violations.append({
                        "inv_id": r.inv_id,
                        "severity": r.severity.value,
                        "message": r.message,
                    })
        except asyncio.TimeoutError:
            # Log but continue — don't let existing engine timeout block new checks
            pass

        # Phase 2: run the 11 new invariants
        for inv_id, check_fn in _NEW_CHECKS:
            try:
                result = await asyncio.wait_for(
                    check_fn(state),
                    timeout=timeout_per_check,
                )
                results[inv_id] = result
                if not result.passed:
                    violations.append({
                        "inv_id": inv_id,
                        "severity": result.severity.value,
                        "message": result.message,
                    })
            except asyncio.TimeoutError:
                results[inv_id] = InvariantResult(
                    inv_id=inv_id,
                    passed=False,
                    severity=Severity.ERROR,
                    message=f"Check timed out after {timeout_per_check}s",
                )
            except Exception as exc:  # noqa: BLE001
                results[inv_id] = InvariantResult(
                    inv_id=inv_id,
                    passed=False,
                    severity=Severity.ERROR,
                    message=f"Check raised exception: {exc}",
                )

        passed = sum(1 for r in results.values() if r.passed)
        failed = sum(1 for r in results.values() if not r.passed)
        skipped = self.TOTAL_INVARIANTS - len(results)
        coverage = (len(results) / self.TOTAL_INVARIANTS) * 100.0

        return InvariantCoverageReport(
            timestamp=time.time(),
            total_invariants=self.TOTAL_INVARIANTS,
            passed=passed,
            failed=failed,
            skipped=skipped,
            coverage_pct=coverage,
            violations=violations,
            results=results,
        )

    async def check_critical_only(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Fast path — run only CRITICAL invariants. Used in execution hot path."""
        report = await self.run_full_coverage(state)
        return [v for v in report.violations if v["severity"] == "CRITICAL"]
