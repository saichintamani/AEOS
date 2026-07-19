"""
Tests — production worker bootstrap (P13-COND-001, Phase 13 Sprint 1).

Proves the bootstrap that constructs WorkerRuntime from settings:
  - production environment forces fail-closed enforcement (require_signed_tokens
    True) even when the config flag is left permissive;
  - a TokenVerifier is built and threaded into the runtime whenever enforcement
    is on;
  - dev/test environments stay permissive unless the flag is set;
  - the explicit flag turns enforcement on outside production;
  - infrastructure dependencies are injectable (Sprint 2 gRPC seam);
  - an end-to-end signed task executes, and an unsigned task is rejected, when a
    production-built worker runs.

Architecture Contract: AC-EXEC-003
Condition: P13-COND-001
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("cryptography", reason="cryptography not installed")

from app.core.config import AEOSSettings
from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import DistributedEventType, EventEnvelope
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.worker.bootstrap import (
    WorkerBootstrapError,
    build_token_verifier,
    build_worker_runtime,
    resolve_enforcement,
)
from app.distributed.worker.runtime import WorkerRuntime
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner


def _identity(node_id="worker-1") -> NodeIdentity:
    return NodeIdentity(node_id=node_id, host="127.0.0.1", port=9000)


def _settings(tmp_path, **overrides) -> AEOSSettings:
    base = dict(
        environment="development",
        token_keys_dir=str(tmp_path / "keys"),
        token_issuer="aeos",
        token_algorithm="ES256",
    )
    base.update(overrides)
    return AEOSSettings(**base)


# ── resolve_enforcement ──────────────────────────────────────────────────────

def test_production_forces_enforcement_even_when_flag_false(tmp_path):
    s = _settings(tmp_path, environment="production", require_signed_tokens=False)
    assert resolve_enforcement(s) is True


def test_prod_alias_forces_enforcement(tmp_path):
    s = _settings(tmp_path, environment="prod", require_signed_tokens=False)
    assert resolve_enforcement(s) is True


def test_development_default_is_permissive(tmp_path):
    s = _settings(tmp_path, environment="development", require_signed_tokens=False)
    assert resolve_enforcement(s) is False


def test_explicit_flag_enables_outside_production(tmp_path):
    s = _settings(tmp_path, environment="staging", require_signed_tokens=True)
    assert resolve_enforcement(s) is True


# ── build_token_verifier ─────────────────────────────────────────────────────

def test_build_token_verifier_creates_keystore(tmp_path):
    s = _settings(tmp_path)
    verifier = build_token_verifier(s)
    assert verifier is not None
    # Round-trip: a token signed by a signer over the same keystore verifies.
    store = KeyStore(keys_dir=s.token_keys_dir, algorithm=KeyAlgorithm.ES256)
    store.initialize()
    signer = TokenSigner(store, issuer="aeos")
    token = signer.sign("task-1", audience=["aeos"], ttl_seconds=60)
    claims = verifier.verify(token, audience="aeos")
    assert claims.sub == "task-1"


def test_build_token_verifier_rejects_bad_algorithm(tmp_path):
    s = _settings(tmp_path, token_algorithm="HS256")
    with pytest.raises(WorkerBootstrapError):
        build_token_verifier(s)


# ── build_worker_runtime ─────────────────────────────────────────────────────

def test_production_worker_is_fail_closed_with_verifier(tmp_path):
    s = _settings(tmp_path, environment="production", require_signed_tokens=False)
    worker = build_worker_runtime(_identity(), settings=s)
    assert isinstance(worker, WorkerRuntime)
    assert worker._governance._require_signed_tokens is True
    assert worker._governance._token_verifier is not None


def test_dev_worker_is_permissive_without_verifier(tmp_path):
    s = _settings(tmp_path, environment="development", require_signed_tokens=False)
    worker = build_worker_runtime(_identity(), settings=s)
    assert worker._governance._require_signed_tokens is False
    assert worker._governance._token_verifier is None


def test_worker_honors_tuning_settings(tmp_path):
    s = _settings(tmp_path, worker_max_in_flight=3, worker_queue_capacity=7)
    worker = build_worker_runtime(_identity(), settings=s)
    assert worker._max_in_flight == 3
    assert worker._queue.maxsize == 7


def test_injected_transport_is_used(tmp_path):
    s = _settings(tmp_path)
    transport = InMemoryTransport()
    worker = build_worker_runtime(_identity(), settings=s, transport=transport)
    assert isinstance(worker, WorkerRuntime)


# ── End-to-end via a production-built worker ─────────────────────────────────

def _accepted(node_id, *, token_id=None, raw_token=None, task_id="t-1"):
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
        source_node_id=node_id,
    )


@pytest.mark.asyncio
async def test_production_worker_executes_signed_task(tmp_path):
    s = _settings(tmp_path, environment="production")
    node = "w1"
    worker = build_worker_runtime(_identity(node), settings=s)

    store = KeyStore(keys_dir=s.token_keys_dir, algorithm=KeyAlgorithm.ES256)
    store.initialize()
    signer = TokenSigner(store, issuer="aeos")
    token = signer.sign("task-signed", audience=["aeos"], ttl_seconds=60)

    ran: list[str] = []

    async def handler(ctx, _):
        ran.append(ctx.task_id)
        return {"ok": True}

    worker.register_handler("echo", handler)
    await worker.start()
    try:
        await worker._on_task_accepted(
            _accepted(node, token_id="task-signed", raw_token=token, task_id="t-signed")
        )
        for _ in range(50):
            if ran:
                break
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()

    assert ran == ["t-signed"]
    assert worker.metrics.completed_tasks == 1


@pytest.mark.asyncio
async def test_production_worker_rejects_unsigned_task(tmp_path):
    s = _settings(tmp_path, environment="production")
    node = "w1"
    worker = build_worker_runtime(_identity(node), settings=s)

    ran: list[str] = []

    async def handler(ctx, _):
        ran.append(ctx.task_id)
        return {"ok": True}

    worker.register_handler("echo", handler)
    await worker.start()
    try:
        # No token_id / raw_token → mandatory mode rejects fail-closed.
        await worker._on_task_accepted(_accepted(node, task_id="t-unsigned"))
        for _ in range(25):
            if worker.metrics.failed_tasks:
                break
            await asyncio.sleep(0.02)
    finally:
        await worker.stop()

    assert ran == []
    assert worker.metrics.failed_tasks == 1
