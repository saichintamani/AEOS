"""
Unit + integration tests — TokenVerifier wired through WorkerRuntime execution.

Closes CRIT-004 (Phase 12A.4 Final Trust Closure). Proves that the JWT
cryptographic verification path is MANDATORY on the runtime execution path,
not merely available as a standalone component.

Covered execution paths (task submission → governance → execution):
  - valid signed token  → task executes
  - expired token       → rejected (reason=expired)
  - tampered signature  → rejected (reason=signature_invalid)
  - malformed token     → rejected (reason=malformed)
  - unknown signing key → rejected (reason=key_not_found)   [missing JWKS key]
  - revoked jti         → rejected (reason=revoked)          [replay after revoke]
  - mandatory mode:
      * missing raw_token → TASK_FAILED, no handler run
      * None token_id     → governance denies

Architecture Contract IDs: AC-EXEC-003
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("cryptography", reason="cryptography not installed")

from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.worker.governance import GovernanceClient, TokenRevokedException
from app.distributed.worker.runtime import WorkerRuntime

from app.security.key_rotation import KeyStore, KeyAlgorithm
from app.security.token_verifier import TokenSigner, TokenVerifier


ISSUER = "aeos"


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    ks = KeyStore(
        keys_dir=str(tmp_path / "keys"),
        algorithm=KeyAlgorithm.ES256,
        key_ttl_seconds=3600,
        overlap_seconds=300,
    )
    ks.initialize()
    return ks


@pytest.fixture
def signer(store):
    return TokenSigner(store, issuer=ISSUER)


@pytest.fixture
def verifier(store):
    return TokenVerifier(store, issuer=ISSUER, clock_skew_seconds=5.0)


def _identity(node_id="worker-1") -> NodeIdentity:
    return NodeIdentity(node_id=node_id, host="127.0.0.1", port=9000)


def _make_worker(node_id="w1", *, token_verifier=None, require_signed_tokens=False):
    transport = InMemoryTransport()
    ser = JsonEventSerializer()
    router = DefaultEventRouter()
    clock = MonotonicClock()
    publisher = DefaultEventPublisher(
        clock=clock, router=router, serializer=ser, transport=transport, source_node_id=node_id
    )
    consumer = DefaultEventConsumer(transport, ser, node_id=node_id)
    lease_mgr = ExecutionLeaseManager(InMemoryLeaseStore())
    cp_engine = CheckpointEngine(InMemoryCheckpointStore())
    worker = WorkerRuntime(
        identity=_identity(node_id),
        publisher=publisher,
        consumer=consumer,
        lease_manager=lease_mgr,
        checkpoint_engine=cp_engine,
        max_in_flight=4,
        heartbeat_interval=9999,
        token_verifier=token_verifier,
        require_signed_tokens=require_signed_tokens,
    )
    return worker, transport, publisher


def _accepted_envelope(node_id="w1", *, token_id=None, raw_token=None, task_id="task-001"):
    payload = {
        "task_id": task_id,
        "workflow_id": "wf-1",
        "step_id": "step-1",
        "task_type": "echo",
        "task_payload": {},
        "priority": "normal",
        "lease_key": "exec:wf-1:step-1",
        "fencing_token": 1,
        "assigned_worker_id": node_id,
        "attempt": 0,
        "max_attempts": 3,
    }
    if token_id is not None:
        payload["token_id"] = token_id
    if raw_token is not None:
        payload["raw_token"] = raw_token
    return EventEnvelope(
        event_type=DistributedEventType.TASK_ACCEPTED,
        payload=payload,
        source_node_id="scheduler",
    )


async def _run_one(worker, envelope):
    """Register a recording handler, drive one task through the worker."""
    completed: list[str] = []

    async def handler(ctx, cb):
        completed.append(ctx.task_id)
        return {"ok": True}

    worker.register_handler("echo", handler)
    await worker._on_task_accepted(envelope)
    await asyncio.sleep(0.05)
    return completed


# ── GovernanceClient unit tests (verify_token with a real verifier) ──────────

class TestGovernanceCryptoVerification:

    def _client(self, verifier=None, require_signed_tokens=False):
        transport = InMemoryTransport()
        ser = JsonEventSerializer()
        consumer = DefaultEventConsumer(transport, ser, node_id="w1")
        return GovernanceClient(
            consumer, "w1",
            token_verifier=verifier,
            require_signed_tokens=require_signed_tokens,
        )

    @pytest.mark.asyncio
    async def test_valid_token_passes(self, signer, verifier):
        client = self._client(verifier)
        token = signer.sign(subject="task-1", gov_approved=True)
        await client.verify_token("task-1", token)  # should not raise

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, signer, verifier):
        client = self._client(verifier)
        token = signer.sign(subject="task-1", ttl_seconds=-3600)  # already expired
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", token)
        assert ei.value.reason == "expired"

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected(self, signer, verifier):
        client = self._client(verifier)
        good = signer.sign(subject="task-1")
        other = signer.sign(subject="task-2")
        # Same key/kid/alg, valid base64 — but signature belongs to a different payload.
        h, p, _ = good.split(".")
        _, _, other_sig = other.split(".")
        forged = f"{h}.{p}.{other_sig}"
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", forged)
        assert ei.value.reason == "signature_invalid"

    @pytest.mark.asyncio
    async def test_malformed_token_rejected(self, verifier):
        client = self._client(verifier)
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", "not-a-jwt")
        assert ei.value.reason == "malformed"

    @pytest.mark.asyncio
    async def test_unknown_key_rejected(self, signer, tmp_path):
        # Verifier backed by a DIFFERENT keystore → kid not found (missing JWKS key).
        other_store = KeyStore(
            keys_dir=str(tmp_path / "other-keys"),
            algorithm=KeyAlgorithm.ES256,
        )
        other_store.initialize()
        foreign_verifier = TokenVerifier(other_store, issuer=ISSUER)
        client = self._client(foreign_verifier)
        token = signer.sign(subject="task-1")
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", token)
        assert ei.value.reason == "key_not_found"

    @pytest.mark.asyncio
    async def test_revoked_jti_rejected_replay(self, signer, verifier):
        """Replay attack: a token accepted once is rejected after its jti is revoked."""
        client = self._client(verifier)
        token = signer.sign(subject="task-1")
        # First use succeeds.
        await client.verify_token("task-1", token)
        # Operator revokes the jti (e.g. via TOKEN_REVOKED event fan-out).
        import json
        from app.security.token_verifier import _b64url_decode
        payload = json.loads(_b64url_decode(token.split(".")[1]))
        verifier.revoke(payload["jti"])
        # Replay now fails closed.
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", token)
        assert ei.value.reason == "revoked"

    @pytest.mark.asyncio
    async def test_mandatory_mode_missing_raw_token(self, verifier):
        client = self._client(verifier, require_signed_tokens=True)
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token("task-1", None)
        assert ei.value.reason == "unsigned_token_rejected"

    @pytest.mark.asyncio
    async def test_mandatory_mode_none_token_rejected(self, verifier):
        client = self._client(verifier, require_signed_tokens=True)
        with pytest.raises(TokenRevokedException) as ei:
            await client.verify_token(None, None)
        assert ei.value.reason == "unsigned_token_rejected"

    @pytest.mark.asyncio
    async def test_permissive_none_token_passes(self, verifier):
        client = self._client(verifier, require_signed_tokens=False)
        await client.verify_token(None, None)  # dev/test path — should not raise


# ── WorkerRuntime end-to-end wiring tests ────────────────────────────────────

class TestWorkerRuntimeTokenWiring:

    @pytest.mark.asyncio
    async def test_valid_token_executes(self, signer, verifier):
        worker, transport, _ = _make_worker(token_verifier=verifier)
        await transport.start()
        await worker.start()
        token = signer.sign(subject="task-001", gov_approved=True)
        env = _accepted_envelope(token_id="task-001", raw_token=token)
        completed = await _run_one(worker, env)
        assert completed == ["task-001"]
        await worker.stop()
        await transport.stop()

    @pytest.mark.asyncio
    async def test_expired_token_blocks_execution(self, signer, verifier):
        worker, transport, _ = _make_worker(token_verifier=verifier)
        await transport.start()
        await worker.start()
        token = signer.sign(subject="task-001", ttl_seconds=-3600)
        env = _accepted_envelope(token_id="task-001", raw_token=token)
        completed = await _run_one(worker, env)
        assert completed == []            # handler never ran
        assert worker.metrics.completed_tasks == 0
        await worker.stop()
        await transport.stop()

    @pytest.mark.asyncio
    async def test_tampered_token_blocks_execution(self, signer, verifier):
        worker, transport, _ = _make_worker(token_verifier=verifier)
        await transport.start()
        await worker.start()
        good = signer.sign(subject="task-001")
        other = signer.sign(subject="task-002")
        h, p, _ = good.split(".")
        forged = f"{h}.{p}.{other.split('.')[2]}"
        env = _accepted_envelope(token_id="task-001", raw_token=forged)
        completed = await _run_one(worker, env)
        assert completed == []
        await worker.stop()
        await transport.stop()

    @pytest.mark.asyncio
    async def test_mandatory_mode_rejects_unsigned_task(self, verifier):
        worker, transport, _ = _make_worker(
            token_verifier=verifier, require_signed_tokens=True
        )
        await transport.start()
        await worker.start()
        # Task with a token_id but NO raw_token — must be rejected fail-closed.
        env = _accepted_envelope(token_id="task-001", raw_token=None)
        completed = await _run_one(worker, env)
        assert completed == []
        assert worker.metrics.failed_tasks >= 1
        await worker.stop()
        await transport.stop()

    @pytest.mark.asyncio
    async def test_permissive_mode_allows_unauthenticated(self, verifier):
        worker, transport, _ = _make_worker(
            token_verifier=verifier, require_signed_tokens=False
        )
        await transport.start()
        await worker.start()
        env = _accepted_envelope(token_id=None, raw_token=None)  # dev/test task
        completed = await _run_one(worker, env)
        assert completed == ["task-001"]
        await worker.stop()
        await transport.stop()
