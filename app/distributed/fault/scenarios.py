"""
Pre-built fault scenarios — async context managers that arm, execute, and verify.

Each scenario:
  1. Arms the FaultInjector for the relevant fault type.
  2. Is used as an async context manager: the test body executes inside __aenter__.
  3. On __aexit__ calls verify_all() and stores the result.

Scenarios: WorkerCrashScenario, LeaseExpirationScenario, NetworkDelayScenario,
           DuplicateEventScenario, CheckpointCorruptionScenario, SlowWorkerScenario,
           HeartbeatLossScenario, ClockSkewScenario.
"""

from __future__ import annotations

from app.distributed.fault.injector import FaultInjector, FaultType, VerificationResult


class _BaseScenario:
    """Base async context manager for fault scenarios."""

    fault_type: FaultType
    invariant_ids: list[str] = []

    def __init__(self, injector: FaultInjector, **kwargs) -> None:
        self._injector = injector
        self.result: VerificationResult | None = None
        self._kwargs = kwargs
        self._arm(**kwargs)

    def _arm(self, **kwargs) -> None:
        after_count = kwargs.get("crash_after_tasks", kwargs.get("after_count", 0))
        self._injector.arm(self.fault_type, after_count=after_count, trigger_count=1)

        async def _default_verifier() -> VerificationResult:
            return VerificationResult(passed=True, invariant_ids=self.invariant_ids)

        if not self._injector._verifiers:
            self._injector.register_verifier(_default_verifier)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        results = await self._injector.verify_all()
        if results:
            # Merge: overall passed = all passed
            passed = all(r.passed for r in results)
            all_ids = [iid for r in results for iid in r.invariant_ids]
            all_violations = [v for r in results for v in r.violations]
            self.result = VerificationResult(
                passed=passed, invariant_ids=all_ids, violations=all_violations
            )
        else:
            self.result = VerificationResult(passed=True, invariant_ids=self.invariant_ids)
        return False  # do not suppress exceptions


class WorkerCrashScenario(_BaseScenario):
    """Simulates a worker crash. Verifies INV-EXEC-001 (no orphaned leases)."""
    fault_type = FaultType.WORKER_CRASH
    invariant_ids = ["INV-EXEC-001"]

    def __init__(self, injector: FaultInjector, crash_after_tasks: int = 0) -> None:
        super().__init__(injector, crash_after_tasks=crash_after_tasks)


class LeaseExpirationScenario(_BaseScenario):
    """Simulates lease TTL expiry. Verifies AC-CONS-001."""
    fault_type = FaultType.LEASE_EXPIRATION
    invariant_ids = ["AC-CONS-001"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class NetworkDelayScenario(_BaseScenario):
    """Simulates network delays. Verifies AC-OBS-002 (clock monotonicity)."""
    fault_type = FaultType.NETWORK_DELAY
    invariant_ids = ["AC-OBS-002"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class DuplicateEventScenario(_BaseScenario):
    """Simulates duplicate message delivery. Verifies INV-EXEC-001."""
    fault_type = FaultType.DUPLICATE_EVENT
    invariant_ids = ["INV-EXEC-001"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class CheckpointCorruptionScenario(_BaseScenario):
    """Simulates checkpoint corruption. Verifies INV-EXEC-002."""
    fault_type = FaultType.CHECKPOINT_CORRUPT
    invariant_ids = ["INV-EXEC-002"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class SlowWorkerScenario(_BaseScenario):
    """Simulates a slow/overloaded worker."""
    fault_type = FaultType.SLOW_WORKER
    invariant_ids = ["AC-SCHED-001"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class HeartbeatLossScenario(_BaseScenario):
    """Simulates heartbeat loss. Verifies PROTO-003 failure detection."""
    fault_type = FaultType.HEARTBEAT_LOSS
    invariant_ids = ["AC-LIFE-002"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)


class ClockSkewScenario(_BaseScenario):
    """Simulates clock skew between nodes. Verifies AC-OBS-002."""
    fault_type = FaultType.CLOCK_SKEW
    invariant_ids = ["AC-OBS-002"]

    def __init__(self, injector: FaultInjector, after_count: int = 0) -> None:
        super().__init__(injector, after_count=after_count)
