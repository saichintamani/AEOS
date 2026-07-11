"""
Governance client — fail-closed token and RBAC verification.

Subscribes to TOKEN_REVOKED, RBAC_REVOKED, POLICY_CHANGED events and
maintains a local revocation cache. verify_token() raises TokenRevokedException
if the token appears in the revocation set (AC-EXEC-003 fail-closed).

None tokens always pass (unauthenticated local tasks).

Protocol: PROTO-015
Contract: AC-EXEC-003
"""

from __future__ import annotations

import asyncio
import logging

from app.distributed.contracts.events import DistributedEventType, EventConsumer, EventEnvelope

logger = logging.getLogger(__name__)


class TokenRevokedException(Exception):
    def __init__(self, token_id: str) -> None:
        super().__init__(f"Token {token_id!r} has been revoked (AC-EXEC-003)")
        self.token_id = token_id


class GovernanceClient:
    """
    Per-worker governance enforcement client.

    Maintains an in-memory revocation cache updated by governance events.
    All verification is fail-closed: unknown → allowed, revoked → raises.
    """

    def __init__(self, consumer: EventConsumer, node_id: str = "") -> None:
        self._consumer = consumer
        self._node_id = node_id
        self._revoked_tokens: set[str] = set()
        self._rbac_revocations: set[tuple[str, str]] = set()  # (principal, resource)
        self._lock = asyncio.Lock()
        self._running = False

    async def start(self) -> None:
        self._running = True
        await self._consumer.subscribe(
            [
                DistributedEventType.TOKEN_REVOKED,
                DistributedEventType.RBAC_REVOKED,
                DistributedEventType.POLICY_CHANGED,
            ],
            self._on_governance_event,
            self._node_id,
        )

    async def stop(self) -> None:
        self._running = False

    async def _on_governance_event(self, envelope: EventEnvelope) -> None:
        if envelope.event_type == DistributedEventType.TOKEN_REVOKED:
            token_id = envelope.payload.get("token_id")
            if token_id:
                async with self._lock:
                    self._revoked_tokens.add(token_id)
                logger.info("Token revoked: %s", token_id)

        elif envelope.event_type == DistributedEventType.RBAC_REVOKED:
            principal = envelope.payload.get("principal", "")
            resource = envelope.payload.get("resource", "")
            if principal and resource:
                async with self._lock:
                    self._rbac_revocations.add((principal, resource))
                logger.info("RBAC revoked: %s → %s", principal, resource)

    async def verify_token(self, token_id: str | None) -> None:
        """Raise TokenRevokedException if the token is in the revocation set."""
        if token_id is None:
            return
        async with self._lock:
            if token_id in self._revoked_tokens:
                raise TokenRevokedException(token_id)

    async def verify_rbac(self, principal: str, resource: str) -> bool:
        """Return False if this principal/resource pair is explicitly revoked."""
        async with self._lock:
            return (principal, resource) not in self._rbac_revocations
