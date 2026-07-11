"""Unit tests — PolicyRuntime, PolicyRegistry."""

from __future__ import annotations

import asyncio
import time
import pytest

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime.policy_runtime import (
    PolicyDefinition,
    PolicyOverride,
    PolicyRegistry,
    PolicyRuntime,
)


def _profile(trust: float = 0.9, region: str = "us-east-1") -> CapabilityProfile:
    return CapabilityProfile(worker_id="w1", trust_score=trust, region=region)


def _req() -> TaskRequirements:
    return TaskRequirements(task_id="t1")


class TestPolicyRuntime:

    @pytest.mark.asyncio
    async def test_allow_by_default_no_policies(self):
        rt = PolicyRuntime()
        verdict = await rt.evaluate(_profile(), _req())
        assert verdict.allowed

    @pytest.mark.asyncio
    async def test_registered_policy_can_deny(self):
        rt = PolicyRuntime()
        await rt.update_policy(PolicyDefinition(
            policy_id="p1",
            name="no-low-trust",
            evaluate=lambda p, r: p.trust_score >= 0.8,
        ))
        verdict = await rt.evaluate(_profile(trust=0.5), _req())
        assert not verdict.allowed
        assert verdict.policy_id == "p1"

    @pytest.mark.asyncio
    async def test_registered_policy_allows_compliant(self):
        rt = PolicyRuntime()
        await rt.update_policy(PolicyDefinition(
            policy_id="p1",
            name="min-trust",
            evaluate=lambda p, r: p.trust_score >= 0.8,
        ))
        verdict = await rt.evaluate(_profile(trust=0.95), _req())
        assert verdict.allowed

    @pytest.mark.asyncio
    async def test_tenant_scoped_policy_not_applied_to_other_tenant(self):
        rt = PolicyRuntime()
        await rt.update_policy(PolicyDefinition(
            policy_id="p1",
            name="tenant-policy",
            evaluate=lambda p, r: False,   # always deny
            tenant_id="tenant-A",
        ))
        # tenant-B should not be affected
        verdict = await rt.evaluate(_profile(), _req(), tenant_id="tenant-B")
        assert verdict.allowed

    @pytest.mark.asyncio
    async def test_override_takes_precedence(self):
        rt = PolicyRuntime()
        # Permissive registered policy
        await rt.update_policy(PolicyDefinition(
            policy_id="p1",
            name="allow-all",
            evaluate=lambda p, r: True,
        ))
        # Restrictive override
        await rt.add_override(PolicyOverride(
            policy_id="override-1",
            evaluate=lambda p, r: False,
            expires_at=time.monotonic() + 3600,
            reason="emergency lockdown",
        ))
        verdict = await rt.evaluate(_profile(), _req())
        assert not verdict.allowed

    @pytest.mark.asyncio
    async def test_expired_override_is_removed(self):
        rt = PolicyRuntime()
        await rt.add_override(PolicyOverride(
            policy_id="override-expired",
            evaluate=lambda p, r: False,
            expires_at=time.monotonic() - 1,   # already expired
        ))
        verdict = await rt.evaluate(_profile(), _req())
        assert verdict.allowed

    @pytest.mark.asyncio
    async def test_remove_override(self):
        rt = PolicyRuntime()
        await rt.add_override(PolicyOverride(
            policy_id="ov-1",
            evaluate=lambda p, r: False,
            expires_at=time.monotonic() + 3600,
        ))
        await rt.remove_override("ov-1")
        verdict = await rt.evaluate(_profile(), _req())
        assert verdict.allowed

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        rt = PolicyRuntime()
        order = []
        await rt.update_policy(PolicyDefinition(
            policy_id="p-low",
            name="low",
            evaluate=lambda p, r: True,
            priority=1,
        ))
        await rt.update_policy(PolicyDefinition(
            policy_id="p-high",
            name="high",
            evaluate=lambda p, r: True,
            priority=10,
        ))
        # Both allow — just verify no error
        verdict = await rt.evaluate(_profile(), _req())
        assert verdict.allowed


class TestPolicyRegistry:

    @pytest.mark.asyncio
    async def test_register_and_get(self):
        reg = PolicyRegistry()
        p = PolicyDefinition(policy_id="p1", name="test", evaluate=lambda a, b: True)
        await reg.register(p)
        result = await reg.get("p1")
        assert result is not None
        assert result.policy_id == "p1"

    @pytest.mark.asyncio
    async def test_unregister(self):
        reg = PolicyRegistry()
        p = PolicyDefinition(policy_id="p1", name="test", evaluate=lambda a, b: True)
        await reg.register(p)
        await reg.unregister("p1")
        assert await reg.get("p1") is None

    @pytest.mark.asyncio
    async def test_list_sorted_by_priority(self):
        reg = PolicyRegistry()
        for i, prio in enumerate([1, 10, 5]):
            await reg.register(PolicyDefinition(
                policy_id=f"p{i}", name=f"p{i}",
                evaluate=lambda a, b: True, priority=prio,
            ))
        policies = await reg.list_policies()
        priorities = [p.priority for p in policies]
        assert priorities == sorted(priorities, reverse=True)
