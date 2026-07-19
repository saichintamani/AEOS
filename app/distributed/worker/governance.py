"""
Governance client — fail-closed token and RBAC verification.

Subscribes to TOKEN_REVOKED, RBAC_REVOKED, POLICY_CHANGED events and
maintains a local revocation cache. verify_token() raises TokenRevokedException
if the token appears in the revocation set (AC-EXEC-003 fail-closed).

When a TokenVerifier is provided at construction, verify_token() also performs
full JWT cryptographic verification (signature, expiry, algorithm, audience).
None tokens always pass (unauthenticated local tasks — only valid in test/dev).

Protocol: PROTO-015
Contract: AC-EXEC-003
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.distributed.contracts.events import DistributedEventType, EventConsumer, EventEnvelope

if TYPE_CHECKING:
    from app.security.token_verifier import TokenVerifier

logger = logging.getLogger(__name__)
# Dedicated audit channel — every governance decision (allow/deny) is emitted
# here with a structured reason so operators can trace authorization outcomes
# independently of application logs. AC-EXEC-003.
audit_logger = logging.getLogger("aeos.audit.governance")


class TokenRevokedException(Exception):
    """
    Raised for ANY governance token rejection (fail-closed, single type).

    ``reason`` carries the specific failure category (expired, revoked,
    malformed, signature_invalid, key_not_found, unsigned_token_rejected)
    so callers and audit logs can distinguish causes without leaking which
    check failed to a remote caller.
    """

    def __init__(self, token_id: str, reason: str = "revoked") -> None:
        super().__init__(
            f"Token {token_id!r} rejected: {reason} (AC-EXEC-003)"
        )
        self.token_id = token_id
        self.reason = reason


class GovernanceClient:
    """
    Per-worker governance enforcement client.

    Maintains an in-memory revocation cache updated by governance events.
    All verification is fail-closed: unknown → allowed, revoked → raises.
    """

    def __init__(
        self,
        consumer: EventConsumer,
        node_id: str = "",
        token_verifier: "TokenVerifier | None" = None,
        *,
        require_signed_tokens: bool = False,
    ) -> None:
        self._consumer = consumer
        self._node_id = node_id
        # Optional cryptographic verifier — when set, all non-None tokens are
        # verified for signature, expiry, and algorithm before revocation check.
        self._token_verifier = token_verifier
        # Mandatory verification mode (production). When True, a task may only
        # execute if it carries a token_id AND a raw_token that verifies against
        # a configured TokenVerifier. Unauthenticated (None) tokens and tokens
        # without a raw JWT are rejected fail-closed. Default False preserves the
        # dev/test path where local tasks run unauthenticated.
        self._require_signed_tokens = require_signed_tokens
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

    async def verify_token(
        self,
        token_id: str | None,
        raw_token: str | None = None,
    ) -> None:
        """
        Verify a governance token (fail-closed).

        Steps (in order):
          1. None token_id → pass in permissive mode (unauthenticated local
             task — dev/test only); rejected in mandatory mode.
          2. Mandatory mode requires a configured verifier + raw_token.
          3. Cryptographic JWT verification via TokenVerifier when configured
             and raw_token is provided (signature, expiry, algorithm, audience).
          4. Revocation check against the local in-memory revocation set.

        Raises TokenRevokedException on any failure so remote callers get a
        single exception type regardless of which check failed; the specific
        cause is preserved in ``.reason`` and the audit log.
        """
        subject = token_id if token_id is not None else "<none>"

        # 1 / 2. Unauthenticated tasks
        if token_id is None:
            if self._require_signed_tokens:
                self._audit_deny(subject, "unsigned_token_rejected",
                                 "mandatory verification: missing token_id")
                raise TokenRevokedException("<none>", "unsigned_token_rejected")
            return

        # Mandatory mode: a signed, verifiable token is required.
        if self._require_signed_tokens:
            if self._token_verifier is None:
                self._audit_deny(subject, "verifier_unavailable",
                                 "mandatory verification: no TokenVerifier configured")
                raise TokenRevokedException(token_id, "verifier_unavailable")
            if raw_token is None:
                self._audit_deny(subject, "unsigned_token_rejected",
                                 "mandatory verification: missing raw_token")
                raise TokenRevokedException(token_id, "unsigned_token_rejected")

        # 3. Cryptographic verification — must pass before revocation check
        if self._token_verifier is not None and raw_token is not None:
            from app.security.token_verifier import (
                TokenError, TokenExpired, TokenRevoked, TokenMalformed,
                TokenSignatureInvalid, TokenKeyNotFound,
            )
            _reason_map = {
                TokenExpired: "expired",
                TokenRevoked: "revoked",
                TokenMalformed: "malformed",
                TokenSignatureInvalid: "signature_invalid",
                TokenKeyNotFound: "key_not_found",
            }
            try:
                self._token_verifier.verify(raw_token, audience="aeos")
            except TokenError as exc:
                reason = next(
                    (r for cls, r in _reason_map.items() if isinstance(exc, cls)),
                    "verification_failed",
                )
                self._audit_deny(subject, reason, str(exc))
                raise TokenRevokedException(token_id, reason) from exc

        # 4. Revocation check
        async with self._lock:
            if token_id in self._revoked_tokens:
                self._audit_deny(subject, "revoked", "token in local revocation set")
                raise TokenRevokedException(token_id, "revoked")

        self._audit_allow(subject)

    def _audit_allow(self, subject: str) -> None:
        audit_logger.info(
            "governance.allow node=%s token=%s", self._node_id, subject
        )

    def _audit_deny(self, subject: str, reason: str, detail: str) -> None:
        audit_logger.warning(
            "governance.deny node=%s token=%s reason=%s detail=%s",
            self._node_id, subject, reason, detail,
        )

    async def verify_rbac(self, principal: str, resource: str) -> bool:
        """Return False if this principal/resource pair is explicitly revoked."""
        async with self._lock:
            return (principal, resource) not in self._rbac_revocations
