"""
FederationServiceServicer — cross-cluster trust and federated task dispatch.

Enables Cluster A to run a task on Cluster B:

  - Handshake → the responder validates algorithm overlap, records the peer's
    identity, and mints a signed FEDERATION SESSION TOKEN (audience
    ["federation"]) that the initiator must present on every later RPC.
  - DispatchFederatedTask → FAIL-CLOSED verification of the session token, then
    the task is handed to an injected ``dispatch_fn(task) -> remote_task_id``
    (normally the local SchedulerServiceServicer). No valid session token → the
    task is rejected (PERMISSION_DENIED) and never dispatched.
  - GetRemoteCapabilities → session-token-gated snapshot of this cluster's fleet
    (idle workers, available task types).
  - WatchFederationEvents → live stream of accepted federated tasks.

Trust model: the session token is signed by THIS cluster and verified by THIS
cluster on subsequent calls (a bearer capability). Cross-cluster JWKS-based
verification of the *initiator's* governance tokens rides on the existing
TokenVerifier federation path and is exercised at the task level, not here.

Phase: 13 Sprint 3
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import grpc

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import worker_pb2 as worker_pb
from aeos.federation.v1 import federation_pb2 as pb
from aeos.federation.v1 import federation_pb2_grpc as pb_grpc

from app.security.token_verifier import TokenError, TokenSigner, TokenVerifier

from ._util import Broadcaster, now_ts, ts_from_epoch

logger = logging.getLogger(__name__)

DispatchFn = Callable[["object"], Awaitable[str]]

_FED_AUDIENCE = "federation"


class FederationServiceServicer(pb_grpc.FederationServiceServicer):
    def __init__(
        self,
        identity: "pb.ClusterIdentity",
        signer: TokenSigner,
        verifier: TokenVerifier,
        *,
        dispatch_fn: DispatchFn | None = None,
        executor: "object | None" = None,
        capabilities_provider: "Callable[[], list[worker_pb.WorkerCapabilities]] | None" = None,
        idle_worker_provider: "Callable[[], int] | None" = None,
        supported_algorithms: "list[str] | None" = None,
        session_ttl_seconds: int = 3600,
    ) -> None:
        self._identity = identity
        self._signer = signer
        self._verifier = verifier
        self._dispatch_fn = dispatch_fn
        self._executor = executor
        self._capabilities_provider = capabilities_provider
        self._idle_worker_provider = idle_worker_provider
        self._algos = supported_algorithms or ["ES256", "RS256"]
        self._session_ttl = session_ttl_seconds
        self._peers: dict[str, pb.ClusterIdentity] = {}
        self._events = Broadcaster()

    async def Handshake(self, request, context):  # noqa: N802
        initiator = request.initiator
        overlap = [a for a in request.supported_algorithms if a in self._algos]
        if not overlap:
            await context.abort(
                grpc.StatusCode.FAILED_PRECONDITION,
                f"no common signing algorithm; local={self._algos} "
                f"remote={list(request.supported_algorithms)}",
            )
        self._peers[initiator.cluster_id] = initiator
        session_token = self._signer.sign(
            subject=initiator.cluster_id,
            audience=[_FED_AUDIENCE],
            ttl_seconds=self._session_ttl,
            extra_claims={"peer_cluster": initiator.cluster_id},
        )
        logger.info("FederationService: handshake accepted from cluster=%s",
                    initiator.cluster_id)
        resp = pb.FederationHandshakeResponse(
            responder=self._identity,
            accepted_algorithms=overlap,
            session_token=session_token,
        )
        try:
            claims = self._verifier.verify(session_token, audience=_FED_AUDIENCE)
            resp.expires_at.CopyFrom(ts_from_epoch(claims.exp))
        except TokenError:  # pragma: no cover - just minted
            pass
        return resp

    async def DispatchFederatedTask(self, request, context):  # noqa: N802
        await self._require_session(request.federation_session_token, context)
        # Prefer the full executor path (real execution + signed evidence); fall
        # back to a bare dispatch_fn (admission only) for callers that wire one.
        if self._executor is not None:
            remote_task_id = await self._executor.dispatch(
                request.task, request.originating_cluster_id)
        elif self._dispatch_fn is not None:
            remote_task_id = await self._dispatch_fn(request.task)
        else:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED,
                                "this cluster accepts no federated tasks")
        resp = pb.FederatedTaskResponse(
            task_id=request.task.task_id,
            remote_task_id=remote_task_id,
            remote_worker_id=request.task.assigned_worker_id,
        )
        resp.dispatched_at.CopyFrom(now_ts())
        await self._events.publish(resp)
        return resp

    async def GetFederatedTaskResult(self, request, context):  # noqa: N802
        await self._require_session(request.federation_session_token, context)
        if self._executor is None:
            await context.abort(grpc.StatusCode.UNIMPLEMENTED,
                                "this cluster does not serve federated results")
        resp = await self._executor.get_result(request.remote_task_id)
        if resp is None:
            await context.abort(grpc.StatusCode.NOT_FOUND,
                                f"unknown federated task {request.remote_task_id}")
        return resp

    async def GetRemoteCapabilities(self, request, context):  # noqa: N802
        await self._require_session(request.federation_session_token, context)
        summary = pb.RemoteCapabilitySummary(cluster_id=self._identity.cluster_id)
        summary.sampled_at.CopyFrom(now_ts())
        if self._idle_worker_provider is not None:
            summary.idle_workers = self._idle_worker_provider()
        task_types: set[str] = set()
        if self._capabilities_provider is not None:
            for caps in self._capabilities_provider():
                task_types.update(caps.supported_task_types)
        summary.available_task_types.extend(sorted(task_types))
        return summary

    async def WatchFederationEvents(self, request, context):  # noqa: N802
        await self._require_session(request.federation_session_token, context)
        q = await self._events.subscribe()
        try:
            while True:
                yield await q.get()
        finally:
            await self._events.unsubscribe(q)

    # ── internals ─────────────────────────────────────────────────────────────
    async def _require_session(self, token: str, context) -> None:
        """Fail-closed session-token gate; aborts the RPC if the token is bad."""
        reason: str | None = None
        if not token:
            reason = "federation_session_token missing"
        else:
            try:
                self._verifier.verify(token, audience=_FED_AUDIENCE)
            except TokenError as exc:
                reason = f"session token rejected: {exc}"
            except Exception as exc:  # pragma: no cover - defensive
                reason = f"session verify error: {exc}"
        if reason is not None:
            logger.warning("FederationService: rejected RPC — %s", reason)
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, reason)
