"""
Fault injector — deterministic fault triggering and invariant verification.

FaultType:   10 named fault categories.
FaultInjector:
  arm(fault_type, after_count, trigger_count)  — configure a fault.
  should_inject(fault_type)                    — check/trigger.
  register_verifier(coro_fn)                   — add a post-fault invariant check.
  verify_all()                                 — run all registered verifiers.
  reset()                                      — clear all state.

Design: deterministic (counter-based), no random delays.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Coroutine


class FaultType(str, Enum):
    WORKER_CRASH         = "worker_crash"
    NETWORK_DELAY        = "network_delay"
    NETWORK_PARTITION    = "network_partition"
    LEASE_EXPIRATION     = "lease_expiration"
    DUPLICATE_EVENT      = "duplicate_event"
    CHECKPOINT_CORRUPT   = "checkpoint_corrupt"
    SLOW_WORKER          = "slow_worker"
    HEARTBEAT_LOSS       = "heartbeat_loss"
    CLOCK_SKEW           = "clock_skew"
    MESSAGE_REORDER      = "message_reorder"


@dataclass
class VerificationResult:
    passed: bool
    invariant_ids: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


Verifier = Callable[[], Coroutine[None, None, VerificationResult]]


@dataclass
class _FaultConfig:
    after_count: int    # calls to skip before triggering
    trigger_count: int  # number of times to trigger


class FaultInjector:
    """Deterministic fault injection framework."""

    def __init__(self) -> None:
        self._configs: dict[FaultType, _FaultConfig] = {}
        self._call_counts: dict[FaultType, int] = {}
        self._trigger_counts: dict[FaultType, int] = {}
        self._verifiers: list[Verifier] = []
        self.trigger_log: list[tuple[FaultType, int]] = []

    def arm(
        self,
        fault_type: FaultType,
        *,
        after_count: int = 0,
        trigger_count: int = 1,
    ) -> None:
        self._configs[fault_type] = _FaultConfig(after_count=after_count, trigger_count=trigger_count)
        self._call_counts[fault_type] = 0
        self._trigger_counts[fault_type] = 0

    async def should_inject(self, fault_type: FaultType) -> bool:
        cfg = self._configs.get(fault_type)
        if cfg is None:
            return False

        count = self._call_counts.get(fault_type, 0)
        triggered = self._trigger_counts.get(fault_type, 0)

        if count < cfg.after_count:
            self._call_counts[fault_type] = count + 1
            return False

        if triggered >= cfg.trigger_count:
            return False

        self._call_counts[fault_type] = count + 1
        self._trigger_counts[fault_type] = triggered + 1
        self.trigger_log.append((fault_type, triggered + 1))
        return True

    def triggered(self, fault_type: FaultType) -> bool:
        return self._trigger_counts.get(fault_type, 0) > 0

    def register_verifier(self, verifier: Verifier) -> None:
        self._verifiers.append(verifier)

    async def verify_all(self) -> list[VerificationResult]:
        results = []
        for v in self._verifiers:
            result = await v()
            results.append(result)
        return results

    def reset(self) -> None:
        self._configs.clear()
        self._call_counts.clear()
        self._trigger_counts.clear()
        self.trigger_log.clear()
