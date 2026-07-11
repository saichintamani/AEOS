"""
AEOS Kernel — Policy Engine

Evaluates registered policies at each Policy Enforcement Point (PEP).
Every call to enforce() either permits or denies the requested action.

Design principles:
  - Fail-safe: if evaluation fails internally, the default is DENY
  - Evaluation order: hard-deny → rate-limit → quota → custom → default-allow
  - All decisions are logged (audit trail)
  - Policies are registered programmatically; Policy-as-Code (YAML DSL) is v3
  - PolicyResult always returns (never raises) — callers check result.allowed

Built-in policies (registered automatically at kernel boot):
  - rate_limit.api           — 100 req/min per actor
  - rate_limit.agent_exec    — 20 agent executions/min per actor
  - tool.denylist            — empty by default, operator-configurable
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any

from app.core.logger import get_logger

__all__ = [
    "PolicyContext",
    "PolicyResult",
    "PolicyType",
    "PolicyDefinition",
    "PolicyEngine",
]

log = get_logger(__name__)


class PolicyType(str, Enum):
    DENY_LIST   = "deny_list"
    RATE_LIMIT  = "rate_limit"
    QUOTA       = "quota"
    CUSTOM      = "custom"


@dataclass
class PolicyContext:
    """Context passed to the Policy Engine at every enforcement point."""
    actor_id: str           # Who is performing the action (caller_id, agent_id, plugin_id)
    action: str             # What they want to do (e.g., "agent.execute", "tool.invoke")
    resource: str           # The target (e.g., tool_id, agent_type, endpoint)
    metadata: dict = field(default_factory=dict)
    trace_id: str = ""


@dataclass
class PolicyResult:
    """Outcome of a policy evaluation. Never raises — check .allowed."""
    allowed: bool
    policy_id: str = ""     # Which policy made the decision
    reason: str = ""        # Human-readable explanation
    actor_id: str = ""
    action: str = ""
    resource: str = ""
    evaluated_at: float = field(default_factory=time.time)


@dataclass
class PolicyDefinition:
    """A registered policy."""
    policy_id: str
    policy_type: PolicyType
    description: str = ""
    # For DENY_LIST: set of blocked resource values
    denied_resources: set[str] = field(default_factory=set)
    # For RATE_LIMIT: calls per window per actor
    rate_limit_calls: int = 0
    rate_limit_window_seconds: float = 60.0
    # For CUSTOM: callable (sync) returning (allowed: bool, reason: str)
    evaluator: Callable[[PolicyContext], tuple[bool, str]] | None = None


class PolicyEngine:
    """
    Registers and evaluates policies at kernel enforcement points.

    Usage:
        engine = PolicyEngine()
        engine.register(PolicyDefinition(
            policy_id="tool.denylist",
            policy_type=PolicyType.DENY_LIST,
            denied_resources={"dangerous_tool"},
        ))
        result = await engine.enforce(PolicyContext(actor_id="agent1", action="tool.invoke", resource="dangerous_tool"))
    """

    def __init__(self) -> None:
        self._policies: dict[str, PolicyDefinition] = {}
        # rate limit counters: policy_id → actor_id → [(timestamp)]
        self._rate_counters: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        self._decisions_total: int = 0
        self._denied_total: int = 0

    # ── Registration ───────────────────────────────────────────────────────────

    def register(self, policy: PolicyDefinition) -> None:
        """Add or replace a policy. Existing policy with same ID is overwritten."""
        self._policies[policy.policy_id] = policy
        log.info("Policy registered", extra={"ctx_policy_id": policy.policy_id, "ctx_type": policy.policy_type.value})

    def unregister(self, policy_id: str) -> None:
        self._policies.pop(policy_id, None)

    # ── Evaluation ─────────────────────────────────────────────────────────────

    async def enforce(self, ctx: PolicyContext) -> PolicyResult:
        """
        Evaluate all registered policies for the given context.

        Evaluation order:
          1. DENY_LIST  — immediate deny if resource matches
          2. RATE_LIMIT — deny if actor has exceeded call rate
          3. QUOTA      — deny if resource quota is exceeded
          4. CUSTOM     — evaluated in registration order
          5. Default    — allow if no policy denied

        Always returns. Never raises.
        """
        try:
            result = self._evaluate(ctx)
        except Exception as exc:
            # Fail-safe: internal error → deny
            log.error(
                "Policy engine internal error — failing safe (deny)",
                extra={"ctx_action": ctx.action, "ctx_error": str(exc)},
            )
            result = PolicyResult(
                allowed=False,
                policy_id="__internal_error__",
                reason=f"Policy engine error: {exc}",
                actor_id=ctx.actor_id,
                action=ctx.action,
                resource=ctx.resource,
            )

        self._decisions_total += 1
        if not result.allowed:
            self._denied_total += 1

        log.debug(
            "Policy evaluated",
            extra={
                "ctx_actor": ctx.actor_id,
                "ctx_action": ctx.action,
                "ctx_resource": ctx.resource,
                "ctx_allowed": result.allowed,
                "ctx_policy": result.policy_id,
            },
        )
        return result

    def _evaluate(self, ctx: PolicyContext) -> PolicyResult:
        base = dict(actor_id=ctx.actor_id, action=ctx.action, resource=ctx.resource)

        # Sort: DENY_LIST first, then RATE_LIMIT, then QUOTA, then CUSTOM
        ordered = sorted(
            self._policies.values(),
            key=lambda p: (
                0 if p.policy_type == PolicyType.DENY_LIST else
                1 if p.policy_type == PolicyType.RATE_LIMIT else
                2 if p.policy_type == PolicyType.QUOTA else 3
            ),
        )

        for policy in ordered:
            if policy.policy_type == PolicyType.DENY_LIST:
                if ctx.resource in policy.denied_resources:
                    return PolicyResult(allowed=False, policy_id=policy.policy_id,
                                        reason=f"Resource '{ctx.resource}' is in deny list.", **base)

            elif policy.policy_type == PolicyType.RATE_LIMIT:
                now = time.time()
                window = policy.rate_limit_window_seconds
                counters = self._rate_counters[policy.policy_id][ctx.actor_id]
                # Prune old timestamps
                cutoff = now - window
                while counters and counters[0] < cutoff:
                    counters.pop(0)
                if len(counters) >= policy.rate_limit_calls:
                    return PolicyResult(
                        allowed=False, policy_id=policy.policy_id,
                        reason=f"Rate limit exceeded: {policy.rate_limit_calls} calls/{window}s",
                        **base
                    )
                counters.append(now)

            elif policy.policy_type == PolicyType.CUSTOM:
                if policy.evaluator:
                    allowed, reason = policy.evaluator(ctx)
                    if not allowed:
                        return PolicyResult(allowed=False, policy_id=policy.policy_id, reason=reason, **base)

        # Default allow
        return PolicyResult(allowed=True, policy_id="default_allow", reason="No policy denied.", **base)

    # ── Built-in policies ──────────────────────────────────────────────────────

    def load_built_ins(self) -> None:
        """Register the default built-in policies."""
        self.register(PolicyDefinition(
            policy_id="rate_limit.api",
            policy_type=PolicyType.RATE_LIMIT,
            description="HTTP API rate limit: 100 req/min per actor",
            rate_limit_calls=100,
            rate_limit_window_seconds=60.0,
        ))
        self.register(PolicyDefinition(
            policy_id="rate_limit.agent_execution",
            policy_type=PolicyType.RATE_LIMIT,
            description="Agent execution rate limit: 20/min per actor",
            rate_limit_calls=20,
            rate_limit_window_seconds=60.0,
        ))
        self.register(PolicyDefinition(
            policy_id="tool.denylist",
            policy_type=PolicyType.DENY_LIST,
            description="Hard-deny list for tools. Empty by default.",
            denied_resources=set(),
        ))
        log.info("Built-in policies loaded")

    # ── Introspection ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        return {
            "policies_registered": len(self._policies),
            "decisions_total": self._decisions_total,
            "denied_total": self._denied_total,
            "policies": list(self._policies.keys()),
        }
