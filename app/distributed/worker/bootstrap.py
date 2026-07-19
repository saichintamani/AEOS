"""
Production worker bootstrap — constructs a WorkerRuntime from settings.

Closes P13-COND-001 (Phase 13 Sprint 1). Until this module existed,
``WorkerRuntime`` was only ever instantiated by tests: token verification
executed on the unconditional hot path, but *mandatory-mode enablement* had no
production entrypoint that set ``require_signed_tokens=True``. This bootstrap is
that entrypoint.

Responsibilities:
  1. Build the cryptographic identity stack (KeyStore → TokenVerifier) from
     settings, so the worker can verify signed governance JWTs.
  2. Decide the enforcement mode. Production profiles are **fail-closed**:
     ``require_signed_tokens`` is forced ``True`` when the environment is
     production, regardless of the raw config flag. Dev/test stay permissive.
  3. Construct WorkerRuntime with those, plus the transport/event/lease/
     checkpoint dependencies.

Transport injection: the transport, publisher, consumer, lease manager, and
checkpoint engine are accepted as optional arguments. When omitted they default
to the in-process implementations. This keeps Sprint 1 unblocked while leaving a
clean seam for the Sprint 2 gRPC inter-node transport to plug in without
touching enforcement logic.

Architecture Contract: AC-EXEC-003
Condition: P13-COND-001
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import AEOSSettings, settings as default_settings
from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.contracts.events import EventConsumer, EventPublisher
from app.distributed.coordination.clock import MonotonicClock
from app.distributed.coordination.lease import InMemoryLeaseStore
from app.distributed.events.consumer import DefaultEventConsumer
from app.distributed.events.publisher import DefaultEventPublisher
from app.distributed.events.router import DefaultEventRouter
from app.distributed.events.serializer import JsonEventSerializer
from app.distributed.execution.checkpoint import CheckpointEngine, InMemoryCheckpointStore
from app.distributed.execution.lease import ExecutionLeaseManager
from app.distributed.transport.memory import InMemoryTransport
from app.distributed.worker.runtime import WorkerRuntime
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenVerifier

if TYPE_CHECKING:
    from app.distributed.contracts.transport import MessageTransport

logger = logging.getLogger(__name__)

_PRODUCTION_ENVIRONMENTS = frozenset({"production", "prod"})


class WorkerBootstrapError(RuntimeError):
    """Raised when a production worker cannot be built fail-closed."""


def _is_production(env: str) -> bool:
    return env.strip().lower() in _PRODUCTION_ENVIRONMENTS


def resolve_enforcement(settings: AEOSSettings) -> bool:
    """
    Decide whether this worker runs in mandatory signed-token mode.

    Production environments are fail-closed: enforcement is on even if the
    operator left ``require_signed_tokens`` at its permissive default. Elsewhere
    the explicit flag wins.
    """
    if _is_production(settings.environment):
        return True
    return bool(settings.require_signed_tokens)


def build_token_verifier(settings: AEOSSettings) -> TokenVerifier:
    """
    Construct a TokenVerifier backed by a persisted KeyStore.

    The KeyStore loads existing signing keys from ``token_keys_dir`` or
    generates an initial pair; the verifier trusts the corresponding public
    keys and enforces the configured issuer and clock skew.
    """
    try:
        algorithm = KeyAlgorithm(settings.token_algorithm)
    except ValueError as exc:
        raise WorkerBootstrapError(
            f"Invalid token_algorithm {settings.token_algorithm!r}; "
            f"expected one of {[a.value for a in KeyAlgorithm]}"
        ) from exc

    store = KeyStore(keys_dir=settings.token_keys_dir, algorithm=algorithm)
    store.initialize()
    return TokenVerifier(
        store,
        issuer=settings.token_issuer,
        clock_skew_seconds=settings.token_clock_skew_seconds,
    )


def build_worker_runtime(
    identity: NodeIdentity,
    *,
    settings: AEOSSettings | None = None,
    transport: "MessageTransport | None" = None,
    publisher: EventPublisher | None = None,
    consumer: EventConsumer | None = None,
    lease_manager: ExecutionLeaseManager | None = None,
    checkpoint_engine: CheckpointEngine | None = None,
    token_verifier: TokenVerifier | None = None,
) -> WorkerRuntime:
    """
    Build a production-configured WorkerRuntime for one node.

    All infrastructure dependencies are injectable (the Sprint 2 gRPC transport
    plugs in here); omitted ones default to the in-process implementations so a
    single-node deployment works out of the box.

    Fail-closed contract: when enforcement resolves to True and no verifier can
    be constructed, this raises ``WorkerBootstrapError`` rather than silently
    downgrading to permissive execution.
    """
    settings = settings or default_settings
    require_signed_tokens = resolve_enforcement(settings)

    if token_verifier is None and require_signed_tokens:
        token_verifier = build_token_verifier(settings)
        if token_verifier is None:  # defensive: build must yield a verifier
            raise WorkerBootstrapError(
                "Mandatory signed-token mode is enabled but no TokenVerifier "
                "could be constructed (refusing to run fail-open)."
            )

    # ── Infrastructure defaults (in-process) ──────────────────────────────────
    if transport is None:
        transport = InMemoryTransport()
    serializer = JsonEventSerializer()
    if publisher is None:
        publisher = DefaultEventPublisher(
            clock=MonotonicClock(),
            router=DefaultEventRouter(),
            serializer=serializer,
            transport=transport,
            source_node_id=identity.node_id,
        )
    if consumer is None:
        consumer = DefaultEventConsumer(transport, serializer, node_id=identity.node_id)
    if lease_manager is None:
        lease_manager = ExecutionLeaseManager(InMemoryLeaseStore())
    if checkpoint_engine is None:
        checkpoint_engine = CheckpointEngine(InMemoryCheckpointStore())

    logger.info(
        "worker.bootstrap node=%s env=%s require_signed_tokens=%s verifier=%s",
        identity.node_id,
        settings.environment,
        require_signed_tokens,
        "present" if token_verifier is not None else "none",
    )

    return WorkerRuntime(
        identity=identity,
        publisher=publisher,
        consumer=consumer,
        lease_manager=lease_manager,
        checkpoint_engine=checkpoint_engine,
        max_in_flight=settings.worker_max_in_flight,
        queue_capacity=settings.worker_queue_capacity,
        heartbeat_interval=settings.worker_heartbeat_interval_seconds,
        token_verifier=token_verifier,
        require_signed_tokens=require_signed_tokens,
    )
