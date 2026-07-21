"""
Remote federated execution — the end-to-end path that turns "clusters trust each
other" into "cluster A runs a workload on cluster B and cryptographically verifies
what B did."

Two ends of one protocol live here:

  * ``FederatedExecutor`` (executing side, cluster B) — admits a dispatched task
    into B's own scheduler, executes it through an injected worker-runtime seam
    (``execute_fn``), records the ``TaskResult``, and mints **execution evidence**:
    a JWT signed by B's key binding {task_id, result hash, originating cluster,
    governance jti, worker, status}. The result + evidence are served back on
    ``GetFederatedTaskResult``.

  * ``FederationClient`` (originating side, cluster A) — handshakes, dispatches,
    polls for the result, and **verifies the trust chain**: B's evidence signature
    against B's published JWKS (no shared secret), that the result hash matches the
    returned result, that the governance jti is the one A minted, and that the
    originating/executing cluster identities are the ones A expects. Any mismatch
    is fail-closed (``FederationTrustError``).

The asymmetry is the whole point: B signs with a private key A never holds; A
verifies with B's public JWKS. A cannot forge B's evidence and B cannot rewrite a
result after signing without invalidating the hash binding.

Phase: 13 Sprint 4
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim)
from aeos.core.v1 import task_pb2 as task_pb
from aeos.federation.v1 import federation_pb2 as pb

from app.security.token_verifier import TokenError, TokenSigner, TokenVerifier

from ._util import now_ts, ts_from_epoch

logger = logging.getLogger(__name__)

# Audience of the signed execution-evidence JWT. Distinct from the "federation"
# session audience so an evidence token can never be replayed as a session token.
EVIDENCE_AUDIENCE = "federation-evidence"

# execute_fn is the worker-runtime seam: given the dispatched Task, run it and
# return a TaskResult. Production injects a WorkerRuntime-backed callable; tests
# inject a deterministic executor.
ExecuteFn = Callable[["task_pb.Task"], Awaitable["task_pb.TaskResult"]]


class FederationTrustError(Exception):
    """Raised on the originating side when B's result fails trust verification."""


def _result_hash(result: "task_pb.TaskResult") -> str:
    """SHA-256 (hex) over the deterministic wire encoding of a TaskResult.

    Binds evidence to this exact result; A recomputes it independently. Uses
    protobuf deterministic serialization so both sides hash identical bytes.
    """
    return hashlib.sha256(result.SerializeToString(deterministic=True)).hexdigest()


def extract_jti(token: str) -> str:
    """Return the ``jti`` claim of a JWT WITHOUT verifying it.

    B echoes the originating governance token's jti into evidence; it does not
    re-sign or trust it. A (which minted the token) checks the echo matches.
    """
    if not token:
        return ""
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        raw = parts[1].encode()
        raw += b"=" * ((-len(raw)) % 4)
        return json.loads(base64.urlsafe_b64decode(raw)).get("jti", "")
    except Exception:  # pragma: no cover - malformed token → no jti
        return ""


def make_echo_executor(worker_id: str = "fed-worker-1", *, delay_s: float = 0.0) -> ExecuteFn:
    """A deterministic worker-runtime seam for tests: echoes the task as SUCCEEDED.

    This stands in for a real WorkerRuntime-backed executor. The federation path
    around it (admission, evidence signing, verification) is identical regardless
    of what execute_fn computes.
    """
    async def _execute(task: "task_pb.Task") -> "task_pb.TaskResult":
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        result = task_pb.TaskResult(
            task_id=task.task_id,
            worker_id=worker_id,
            status=task_pb.TASK_STATUS_SUCCEEDED,
            attempt=1,
        )
        result.started_at.CopyFrom(now_ts())
        result.completed_at.CopyFrom(now_ts())
        return result
    return _execute


@dataclass
class _RemoteState:
    task: "task_pb.Task"
    originating_cluster_id: str
    worker_id: str
    ready: bool = False
    result: "task_pb.TaskResult | None" = None
    evidence: "pb.ExecutionEvidence | None" = None
    task_ref: "asyncio.Task | None" = field(default=None, repr=False)


class FederatedExecutor:
    """Executing side (cluster B): run dispatched tasks and sign evidence."""

    def __init__(
        self,
        *,
        cluster_id: str,
        signer: TokenSigner,
        execute_fn: ExecuteFn,
        scheduler: "object | None" = None,
        evidence_ttl_seconds: int = 3600,
    ) -> None:
        self._cluster_id = cluster_id
        self._signer = signer
        self._execute_fn = execute_fn
        self._scheduler = scheduler
        self._evidence_ttl = evidence_ttl_seconds
        self._states: dict[str, _RemoteState] = {}
        self._lock = asyncio.Lock()

    async def dispatch(self, task: "task_pb.Task", originating_cluster_id: str) -> str:
        """Admit + start executing a federated task; return its remote id.

        Idempotent per task: a duplicate dispatch of the same task_id returns the
        existing remote id and does not start a second execution.
        """
        remote_task_id = f"{self._cluster_id}-{task.task_id}"
        async with self._lock:
            existing = self._states.get(remote_task_id)
            if existing is not None:
                return remote_task_id
            worker_id = task.assigned_worker_id or "fed-worker-1"
            state = _RemoteState(
                task=task, originating_cluster_id=originating_cluster_id,
                worker_id=worker_id,
            )
            self._states[remote_task_id] = state

        # Route into B's own scheduler registry (real local admission).
        if self._scheduler is not None:
            try:
                assigned = await self._scheduler.admit(task)
                if assigned:
                    state.worker_id = assigned
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("FederatedExecutor: scheduler admit failed: %s", exc)

        state.task_ref = asyncio.ensure_future(self._run(remote_task_id, state))
        return remote_task_id

    async def _run(self, remote_task_id: str, state: _RemoteState) -> None:
        task = state.task
        try:
            result = await self._execute_fn(task)
        except Exception as exc:
            result = task_pb.TaskResult(
                task_id=task.task_id, worker_id=state.worker_id,
                status=task_pb.TASK_STATUS_FAILED,
                error=task_pb.TaskError(code="executor_error", message=str(exc)),
                attempt=1,
            )
            result.completed_at.CopyFrom(now_ts())
        if not result.worker_id:
            result.worker_id = state.worker_id

        if self._scheduler is not None:
            try:
                await self._scheduler.report_result(result)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("FederatedExecutor: report_result failed: %s", exc)

        evidence = self._sign_evidence(state, result)
        async with self._lock:
            state.result = result
            state.evidence = evidence
            state.ready = True
        logger.info("FederatedExecutor: completed %s status=%s", remote_task_id, result.status)

    def _sign_evidence(
        self, state: _RemoteState, result: "task_pb.TaskResult",
    ) -> "pb.ExecutionEvidence":
        result_hash = _result_hash(result)
        gov_jti = extract_jti(state.task.governance_token)
        claims = {
            "executing_cluster": self._cluster_id,
            "originating_cluster": state.originating_cluster_id,
            "worker_id": result.worker_id,
            "status": int(result.status),
            "result_hash": result_hash,
            "governance_jti": gov_jti,
        }
        evidence_token = self._signer.sign(
            subject=state.task.task_id,
            audience=[EVIDENCE_AUDIENCE],
            ttl_seconds=self._evidence_ttl,
            extra_claims=claims,
        )
        ev = pb.ExecutionEvidence(
            task_id=state.task.task_id,
            executing_cluster_id=self._cluster_id,
            originating_cluster_id=state.originating_cluster_id,
            worker_id=result.worker_id,
            status=result.status,
            result_hash=result_hash,
            governance_jti=gov_jti,
            evidence_token=evidence_token,
        )
        if result.HasField("started_at"):
            ev.started_at.CopyFrom(result.started_at)
        if result.HasField("completed_at"):
            ev.completed_at.CopyFrom(result.completed_at)
        return ev

    async def get_result(self, remote_task_id: str) -> "pb.FederatedTaskResultResponse | None":
        """Return the current result state, or None if the id is unknown."""
        async with self._lock:
            state = self._states.get(remote_task_id)
            if state is None:
                return None
            resp = pb.FederatedTaskResultResponse(
                task_id=state.task.task_id,
                remote_task_id=remote_task_id,
                ready=state.ready,
            )
            if state.ready and state.result is not None:
                resp.result.CopyFrom(state.result)
                if state.evidence is not None:
                    resp.evidence.CopyFrom(state.evidence)
            return resp


# ── originating side (cluster A) ───────────────────────────────────────────────

class FederationClient:
    """Originating side (cluster A): dispatch to a peer and verify what it did."""

    def __init__(self, fed_stub, *, originating_cluster_id: str) -> None:
        self._stub = fed_stub
        self._cluster_id = originating_cluster_id

    async def handshake(
        self, initiator_identity, *, algorithms: "list[str] | None" = None,
    ) -> str:
        resp = await self._stub.Handshake(pb.FederationHandshakeRequest(
            initiator=initiator_identity,
            supported_algorithms=algorithms or ["ES256", "RS256"],
        ))
        return resp.session_token

    async def dispatch(self, task, session_token: str) -> str:
        resp = await self._stub.DispatchFederatedTask(pb.FederatedTaskRequest(
            task=task,
            originating_cluster_id=self._cluster_id,
            federation_session_token=session_token,
        ))
        return resp.remote_task_id

    async def await_result(
        self, remote_task_id: str, session_token: str,
        *, timeout: float = 5.0, poll_interval: float = 0.02,
    ) -> "pb.FederatedTaskResultResponse":
        """Poll GetFederatedTaskResult until ready; raise TimeoutError past deadline."""
        deadline = time.monotonic() + timeout
        while True:
            resp = await self._stub.GetFederatedTaskResult(
                pb.GetFederatedTaskResultRequest(
                    remote_task_id=remote_task_id,
                    originating_cluster_id=self._cluster_id,
                    federation_session_token=session_token,
                ))
            if resp.ready:
                return resp
            if time.monotonic() >= deadline:
                raise TimeoutError(f"federated result {remote_task_id} not ready in {timeout}s")
            await asyncio.sleep(poll_interval)

    @staticmethod
    def verify_evidence(
        response: "pb.FederatedTaskResultResponse",
        *,
        peer_verifier: TokenVerifier,
        expected_governance_jti: str,
        expected_executing_cluster: str,
        expected_originating_cluster: str,
    ) -> "pb.ExecutionEvidence":
        """Fail-closed trust check of B's signed result. Returns evidence on success.

        Verifies, in order:
          1. B's evidence-token signature (against B's JWKS) + issuer/audience;
          2. the result hash in the evidence matches the returned result bytes;
          3. the signed claims match the evidence-message fields (no tampering
             of the plaintext envelope around the signed token);
          4. governance jti equals the token A minted (B ran A's authorized task);
          5. executing/originating cluster identities match expectations.
        Any failure raises FederationTrustError.
        """
        ev = response.evidence
        if not ev.evidence_token:
            raise FederationTrustError("no execution evidence token present")

        # (1) signature + issuer + audience
        try:
            claims = peer_verifier.verify(ev.evidence_token, audience=EVIDENCE_AUDIENCE)
        except TokenError as exc:
            raise FederationTrustError(f"evidence signature/claims rejected: {exc}") from exc

        # (2) result hash binds the signature to these exact result bytes
        recomputed = _result_hash(response.result)
        if recomputed != ev.result_hash:
            raise FederationTrustError(
                f"result hash mismatch: evidence={ev.result_hash} recomputed={recomputed}")
        if claims.extra.get("result_hash") != recomputed:
            raise FederationTrustError("signed result_hash does not match the returned result")

        # (3) the plaintext envelope must agree with the signed claims
        if claims.sub != ev.task_id or ev.task_id != response.task_id:
            raise FederationTrustError("task_id mismatch between token, evidence, and response")
        if claims.extra.get("executing_cluster") != ev.executing_cluster_id:
            raise FederationTrustError("executing cluster mismatch (envelope vs signed)")
        if claims.extra.get("governance_jti") != ev.governance_jti:
            raise FederationTrustError("governance jti mismatch (envelope vs signed)")

        # (4) governance binding: B ran the task A authorized
        if not expected_governance_jti:
            raise FederationTrustError("no expected governance jti supplied")
        if ev.governance_jti != expected_governance_jti:
            raise FederationTrustError(
                f"governance jti mismatch: expected={expected_governance_jti} got={ev.governance_jti}")

        # (5) identity bindings
        if ev.executing_cluster_id != expected_executing_cluster:
            raise FederationTrustError(
                f"executing cluster mismatch: expected={expected_executing_cluster} "
                f"got={ev.executing_cluster_id}")
        if ev.originating_cluster_id != expected_originating_cluster:
            raise FederationTrustError(
                f"originating cluster mismatch: expected={expected_originating_cluster} "
                f"got={ev.originating_cluster_id}")

        return ev
