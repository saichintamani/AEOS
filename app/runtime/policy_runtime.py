"""
Wave 9B.4.5 — Dynamic Policy Engine

Policies are no longer static. They can be:
  - Updated at runtime without restart
  - Temporarily overridden (time-bounded)
  - Tenant-specific
  - Emergency restrictions

PolicyRegistry   — stores active policies, supports hot-swap
PolicyRuntime    — evaluates policies for a given (task, worker) pair
OverrideContext  — temporary policy override with expiry
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)

PolicyFn = Callable[[CapabilityProfile, TaskRequirements], bool]   # True = allow


@dataclass
class PolicyDefinition:
    policy_id: str
    name: str
    evaluate: PolicyFn
    priority: int = 0         # higher = evaluated first
    tenant_id: str = ""       # empty = applies to all tenants
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyOverride:
    policy_id: str
    evaluate: PolicyFn
    expires_at: float         # monotonic time
    reason: str = ""


@dataclass
class PolicyVerdict:
    allowed: bool
    policy_id: str = ""
    reason: str = ""


class PolicyRegistry:
    """Thread-safe registry of PolicyDefinitions."""

    def __init__(self) -> None:
        self._policies: dict[str, PolicyDefinition] = {}
        self._lock = asyncio.Lock()

    async def register(self, policy: PolicyDefinition) -> None:
        async with self._lock:
            self._policies[policy.policy_id] = policy
            logger.debug("PolicyRegistry: registered policy '%s'", policy.policy_id)

    async def unregister(self, policy_id: str) -> None:
        async with self._lock:
            self._policies.pop(policy_id, None)

    async def list_policies(self) -> list[PolicyDefinition]:
        async with self._lock:
            return sorted(self._policies.values(), key=lambda p: p.priority, reverse=True)

    async def get(self, policy_id: str) -> PolicyDefinition | None:
        async with self._lock:
            return self._policies.get(policy_id)


class PolicyRuntime:
    """
    Evaluates all registered policies for a (worker, task) pair.

    Policy chain: overrides → tenant-specific → global.
    First DENY short-circuits evaluation.
    """

    def __init__(
        self,
        registry: PolicyRegistry | None = None,
        telemetry_bus: TelemetryBus | None = None,
    ) -> None:
        self._registry = registry or PolicyRegistry()
        self._overrides: dict[str, PolicyOverride] = {}
        self._bus = telemetry_bus
        self._lock = asyncio.Lock()

    async def add_override(self, override: PolicyOverride) -> None:
        async with self._lock:
            self._overrides[override.policy_id] = override
            logger.info(
                "PolicyRuntime: override added for policy '%s' — reason: %s",
                override.policy_id, override.reason,
            )
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.POLICY_UPDATED,
                source="PolicyRuntime",
                payload={
                    "policy_id": override.policy_id,
                    "type": "override",
                    "reason": override.reason,
                },
            ))

    async def remove_override(self, policy_id: str) -> None:
        async with self._lock:
            self._overrides.pop(policy_id, None)

    async def evaluate(
        self,
        profile: CapabilityProfile,
        requirements: TaskRequirements,
        tenant_id: str = "",
    ) -> PolicyVerdict:
        now = time.monotonic()

        # Expire stale overrides
        async with self._lock:
            expired = [pid for pid, ov in self._overrides.items() if ov.expires_at <= now]
            for pid in expired:
                del self._overrides[pid]
            overrides = dict(self._overrides)

        # Check active overrides first
        for policy_id, override in overrides.items():
            try:
                if not override.evaluate(profile, requirements):
                    self._emit_reject(policy_id, "override", requirements)
                    return PolicyVerdict(allowed=False, policy_id=policy_id, reason="override")
            except Exception:
                logger.exception("PolicyRuntime: override evaluation error for %s", policy_id)

        # Check registered policies
        policies = await self._registry.list_policies()
        for policy in policies:
            if policy.tenant_id and policy.tenant_id != tenant_id:
                continue
            try:
                if not policy.evaluate(profile, requirements):
                    self._emit_reject(policy.policy_id, policy.name, requirements)
                    return PolicyVerdict(
                        allowed=False, policy_id=policy.policy_id, reason=policy.name
                    )
            except Exception:
                logger.exception("PolicyRuntime: policy evaluation error for %s", policy.policy_id)

        return PolicyVerdict(allowed=True)

    async def update_policy(self, policy: PolicyDefinition) -> None:
        """Hot-swap a policy without restart."""
        await self._registry.register(policy)
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.POLICY_UPDATED,
                source="PolicyRuntime",
                payload={"policy_id": policy.policy_id, "type": "update"},
            ))

    def _emit_reject(self, policy_id: str, reason: str, req: TaskRequirements) -> None:
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.GOVERNANCE_REJECTED,
                source="PolicyRuntime",
                payload={
                    "policy_id": policy_id,
                    "reason": reason,
                    "task_id": req.task_id,
                },
                correlation_id=req.task_id,
            ))
