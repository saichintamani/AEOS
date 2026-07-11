"""
Execution lease manager with fencing tokens.

Fencing tokens are monotonically increasing integers that prevent split-brain
execution. Any holder presenting a token value lower than the currently stored
value is rejected (INV-EXEC-001).

Protocol: PROTO-019
Contract: AC-CONS-001
ADR: ADR-009
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.distributed.contracts.coordination import LeaseStore


class StaleFencingTokenError(Exception):
    """Raised when an operation is attempted with a token that has been superseded."""

    def __init__(self, lease_key: str, token_value: int, current_value: int) -> None:
        self.lease_key = lease_key
        self.token_value = token_value
        self.current_value = current_value
        super().__init__(
            f"Stale fencing token on {lease_key!r}: "
            f"presented={token_value}, current={current_value} (INV-EXEC-001)"
        )


@dataclass(frozen=True)
class FencingToken:
    """An opaque, monotonically increasing execution lease credential."""
    lease_key: str
    value: int
    holder_id: str


class ExecutionLeaseManager:
    """
    Wraps LeaseStore and adds fencing-token-based split-brain prevention.

    acquire() → issues a FencingToken with a value higher than any previously
                issued for this lease key.
    verify()  → returns False if the token's value is stale.
    release() → raises StaleFencingTokenError if the token is stale.
    steal()   → forcibly acquires from a new holder, invalidating prior tokens.
    """

    _DEFAULT_TTL = 120

    def __init__(
        self,
        store: LeaseStore,
        *,
        default_ttl_seconds: int = _DEFAULT_TTL,
    ) -> None:
        self._store = store
        self._default_ttl = default_ttl_seconds
        self._fence_counters: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def _next_fence_value(self, lease_key: str) -> int:
        async with self._lock:
            self._fence_counters[lease_key] = self._fence_counters.get(lease_key, 0) + 1
            return self._fence_counters[lease_key]

    async def _current_fence_value(self, lease_key: str) -> int:
        async with self._lock:
            return self._fence_counters.get(lease_key, 0)

    async def acquire(
        self,
        lease_key: str,
        holder_id: str,
        *,
        ttl_seconds: int | None = None,
        auto_renew: bool = True,
    ) -> FencingToken | None:
        ttl = ttl_seconds or self._default_ttl
        record = await self._store.acquire(lease_key, holder_id, ttl)
        if record is None:
            return None
        value = await self._next_fence_value(lease_key)
        return FencingToken(lease_key=lease_key, value=value, holder_id=holder_id)

    async def renew(self, token: FencingToken) -> bool:
        current = await self._current_fence_value(token.lease_key)
        if token.value < current:
            return False
        return await self._store.renew(token.lease_key, token.holder_id, self._default_ttl)

    async def release(self, token: FencingToken) -> bool:
        current = await self._current_fence_value(token.lease_key)
        if token.value < current:
            raise StaleFencingTokenError(token.lease_key, token.value, current)
        return await self._store.release(token.lease_key, token.holder_id)

    async def verify(self, token: FencingToken) -> bool:
        """Return True if the token is still the current holder with a valid fence value."""
        current = await self._current_fence_value(token.lease_key)
        if token.value < current:
            return False
        return await self._store.is_held_by(token.lease_key, token.holder_id)

    async def steal(self, lease_key: str, new_holder_id: str) -> FencingToken | None:
        """
        Forcibly acquire a lease from any current holder.

        Used by RecoveryRuntime to take over an expired/crashed worker's lease.
        Increments the fence counter, invalidating all prior tokens.
        """
        # Force-release existing lease by deleting directly through store
        existing = await self._store.get(lease_key)
        if existing:
            try:
                await self._store.release(lease_key, existing.holder_id)
            except Exception:
                pass  # already expired or released

        record = await self._store.acquire(lease_key, new_holder_id, self._default_ttl)
        if record is None:
            return None
        value = await self._next_fence_value(lease_key)
        return FencingToken(lease_key=lease_key, value=value, holder_id=new_holder_id)
