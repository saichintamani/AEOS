"""
GovernanceServiceServicer — the authoritative execution-authorization service.

This is the wire realization of GovernanceService (proto/aeos/governance/v1).
It delegates to the real security primitives already in AEOS:

  - RequestApproval → evaluate a policy, and on APPROVE mint a signed JWT via
    ``TokenSigner`` (RS256/ES256). No token is ever minted for a DENIED request.
  - VerifyToken   → ``TokenVerifier.verify`` — FAIL-CLOSED: any TokenError
    (expired, bad signature, revoked, wrong audience) returns valid=False, never
    an exception across the wire and never a false "valid".
  - RevokeToken   → ``TokenVerifier.revoke`` (jti added to the revocation set).
  - QueryAuditLog → immutable in-memory audit trail of every decision.
  - WatchGovernanceEvents → live stream of audit records to dashboards/SIEM.

Policy hook: ``approve_fn(request) -> (approved: bool, reason: str)`` lets a
caller inject real policy. The default approves every well-formed request
(subject_id present) and denies the rest — the token machinery is what is being
proven here, not a policy engine.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.governance.v1 import governance_pb2 as pb
from aeos.governance.v1 import governance_pb2_grpc as pb_grpc

from app.security.token_verifier import TokenError, TokenSigner, TokenVerifier

from ._util import Broadcaster, now_ts, ts_from_epoch

logger = logging.getLogger(__name__)

ApprovalPolicy = Callable[["pb.ApprovalRequest"], "tuple[bool, str]"]


def _default_policy(request: "pb.ApprovalRequest") -> tuple[bool, str]:
    if not request.subject_id:
        return False, "subject_id is required"
    return True, "default policy: approved"


class GovernanceServiceServicer(pb_grpc.GovernanceServiceServicer):
    def __init__(
        self,
        signer: TokenSigner,
        verifier: TokenVerifier,
        *,
        policy: ApprovalPolicy | None = None,
        issuing_cluster_id: str = "aeos",
        default_ttl_seconds: int = 300,
        max_ttl_seconds: int = 3600,
    ) -> None:
        self._signer = signer
        self._verifier = verifier
        self._policy = policy or _default_policy
        self._cluster_id = issuing_cluster_id
        self._default_ttl = default_ttl_seconds
        self._max_ttl = max_ttl_seconds
        self._audit: list[pb.AuditRecord] = []
        self._events = Broadcaster()

    # ── decision path ─────────────────────────────────────────────────────────
    async def RequestApproval(self, request, context):  # noqa: N802
        approved, reason = self._policy(request)
        decision = (
            pb.APPROVAL_DECISION_APPROVED if approved else pb.APPROVAL_DECISION_DENIED
        )
        resp = pb.ApprovalResponse(
            request_id=request.request_id,
            decision=decision,
            reason=reason,
            policy_id=request.policy_id,
        )
        if approved:
            ttl = request.ttl_seconds or self._default_ttl
            ttl = min(ttl, self._max_ttl)
            token = self._signer.sign(
                subject=request.subject_id,
                audience=["aeos"],
                ttl_seconds=ttl,
                gov_approved=True,
                workflow_id=request.subject_id if request.subject_type == "workflow" else "",
            )
            resp.governance_token = token
            # Echo the exp so the caller need not decode the JWT.
            try:
                claims = self._verifier.verify(token, audience="aeos")
                resp.expires_at.CopyFrom(ts_from_epoch(claims.exp))
            except TokenError:  # pragma: no cover - we just minted it
                pass
        await self._record(request, decision, reason)
        return resp

    # ── verification path (fail-closed) ───────────────────────────────────────
    async def VerifyToken(self, request, context):  # noqa: N802
        audience = list(request.expected_audience) or None
        try:
            claims = self._verifier.verify(request.governance_token, audience=audience)
        except TokenError as exc:
            # Fail-closed: a bad token yields valid=False, not an RPC error.
            return pb.VerifyTokenResponse(valid=False, failure_reason=str(exc))
        except Exception as exc:  # pragma: no cover - defensive fail-closed
            return pb.VerifyTokenResponse(valid=False, failure_reason=f"verify error: {exc}")
        resp = pb.VerifyTokenResponse(
            valid=True,
            subject_id=claims.sub,
            issuer=claims.iss,
        )
        resp.issued_at.CopyFrom(ts_from_epoch(claims.iat))
        resp.expires_at.CopyFrom(ts_from_epoch(claims.exp))
        return resp

    async def RevokeToken(self, request, context):  # noqa: N802
        if not request.jti:
            return pb.RevokeTokenResponse(revoked=False)
        self._verifier.revoke(request.jti)
        logger.info("GovernanceService: revoked jti=%s reason=%s",
                    request.jti[:8], request.reason)
        return pb.RevokeTokenResponse(revoked=True)

    # ── audit ─────────────────────────────────────────────────────────────────
    async def QueryAuditLog(self, request, context):  # noqa: N802
        records = self._filter_audit(request)
        limit = request.limit or len(records)
        page = records[:limit]
        return pb.AuditQueryResponse(
            records=page,
            has_more=len(records) > len(page),
            next_cursor="",
        )

    async def WatchGovernanceEvents(self, request, context):  # noqa: N802
        # Replay matching history, then stream live decisions.
        for rec in self._filter_audit(request):
            yield rec
        q = await self._events.subscribe()
        try:
            while True:
                rec = await q.get()
                if self._matches(rec, request):
                    yield rec
        finally:
            await self._events.unsubscribe(q)

    # ── internals ─────────────────────────────────────────────────────────────
    async def _record(self, request, decision, reason) -> None:
        rec = pb.AuditRecord(
            record_id=str(uuid.uuid4()),
            request_id=request.request_id,
            subject_id=request.subject_id,
            requester_id=request.requester_id,
            decision=decision,
            policy_id=request.policy_id,
            reason=reason,
            issuing_cluster_id=self._cluster_id,
        )
        rec.decided_at.CopyFrom(now_ts())
        self._audit.append(rec)
        await self._events.publish(rec)

    def _filter_audit(self, request) -> list[pb.AuditRecord]:
        return [r for r in self._audit if self._matches(r, request)]

    @staticmethod
    def _matches(rec: pb.AuditRecord, request) -> bool:
        if getattr(request, "subject_id", "") and rec.subject_id != request.subject_id:
            return False
        if getattr(request, "requester_id", "") and rec.requester_id != request.requester_id:
            return False
        if getattr(request, "policy_id", "") and rec.policy_id != request.policy_id:
            return False
        return True
