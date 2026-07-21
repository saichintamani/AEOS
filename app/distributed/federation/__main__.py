"""
Federation gateway entrypoint — ``python -m app.distributed.federation``.

Closes doc-033 H1 (federation was undeployable: the FederationService, its
scheduler admission path, and the JWKS endpoint were only ever composed inside
the certification harness — no production process stood them up). This module is
that process. It is *deployability glue only*: every component it wires
(``KeyStore`` → ``TokenSigner``/``TokenVerifier``, ``SchedulerServiceServicer``,
``FederationServiceServicer``, ``DomainServiceServer``, ``JWKSProvider``) already
exists and is exercised by tests. No new federation capability is added.

What this gateway provides (the federation CONTROL plane):
  1. gRPC ``FederationService`` on ``FEDERATION_GRPC_PORT`` (default 50051):
     Handshake (algorithm overlap → signed session token), fail-closed session
     verification, and trust-gated admission of federated tasks into THIS
     cluster's local ``SchedulerServiceServicer`` (via its ``admit`` seam).
  2. JWKS exposure: ``GET /.well-known/jwks.json`` on ``FEDERATION_HTTP_PORT``
     (default 8080) serving this cluster's public verification keys, so peers
     can verify tokens/evidence this cluster signs (no shared secret).
  3. Probes: ``GET /health`` (liveness) and ``GET /health/ready`` (readiness).

What this gateway does NOT do: it does not itself EXECUTE remote tasks. Remote
execution requires a production ``WorkerRuntime``-backed ``execute_fn`` seam for
``FederatedExecutor``; the only ``execute_fn`` in the tree today is the test
``make_echo_executor``. Wiring a real execution bridge would be a new feature and
is intentionally out of scope here. The gateway therefore runs the servicer in
its admission-only (``dispatch_fn``) mode: it terminates federation trust and
routes authorized tasks into the local scheduler registry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from aiohttp import web

from app.core.config import settings
from app.distributed.worker.bootstrap import resolve_enforcement
from app.security.jwks import JWKSProvider
from app.security.key_rotation import KeyAlgorithm, KeyStore
from app.security.token_verifier import TokenSigner, TokenVerifier

import app.distributed.grpc.generated  # noqa: F401  (sys.path shim for aeos.* protos)
from aeos.federation.v1 import federation_pb2 as fed_pb

from app.distributed.grpc.services import (
    DomainServiceServer,
    FederationServiceServicer,
    SchedulerServiceServicer,
)

logger = logging.getLogger(__name__)

_GRPC_PORT = int(os.getenv("FEDERATION_GRPC_PORT", "50051"))
_HTTP_PORT = int(os.getenv("FEDERATION_HTTP_PORT", "8080"))


def _make_http_app(server: DomainServiceServer, jwks: JWKSProvider) -> web.Application:
    """Probe + JWKS endpoints the federation manifests reference."""
    app = web.Application()

    async def health(_req: web.Request) -> web.Response:
        return web.json_response({
            "status": "healthy" if server.is_running else "starting",
            "environment": settings.environment,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def health_ready(_req: web.Request) -> web.Response:
        ready = server.is_running
        return web.json_response({"ready": ready}, status=200 if ready else 503)

    async def jwks_handler(_req: web.Request) -> web.Response:
        return web.json_response(jwks.jwks_dict())

    app.router.add_get("/health", health)
    app.router.add_get("/health/ready", health_ready)
    app.router.add_get("/.well-known/jwks.json", jwks_handler)
    return app


async def _run() -> None:
    # Cluster identity: the federation cluster_id doubles as the token issuer so
    # a peer verifying this cluster's JWKS uses the same `iss`. Defaults keep the
    # single-cluster case working with the standard settings.token_issuer.
    cluster_id = os.getenv("FEDERATION_CLUSTER_ID") or settings.token_issuer
    region = os.getenv("FEDERATION_REGION", "")
    advertised_jwks_url = os.getenv("FEDERATION_JWKS_URL", "")
    host = os.getenv("FEDERATION_BIND_HOST", "0.0.0.0")

    # Production key stack (same persisted KeyStore the worker bootstrap uses).
    try:
        algorithm = KeyAlgorithm(settings.token_algorithm)
    except ValueError:
        algorithm = KeyAlgorithm.ES256
    store = KeyStore(keys_dir=settings.token_keys_dir, algorithm=algorithm)
    store.initialize()
    signer = TokenSigner(store, issuer=cluster_id)
    # Issuer aligns with cluster_id so a peer verifying this cluster's JWKS uses
    # the same `iss` it expects for this cluster.
    verifier = TokenVerifier(
        store,
        issuer=cluster_id,
        clock_skew_seconds=settings.token_clock_skew_seconds,
    )

    # Fail-closed in production: federated tasks must carry a governance token.
    require_governance = resolve_enforcement(settings)
    scheduler = SchedulerServiceServicer(
        verifier=verifier, require_governance=require_governance,
    )

    identity = fed_pb.ClusterIdentity(
        cluster_id=cluster_id,
        display_name=cluster_id,
        region=region,
        jwks_url=advertised_jwks_url,
    )
    # Admission-only mode: trust termination + route into the local scheduler.
    # (No executor: real remote execution needs a WorkerRuntime execute_fn seam
    # that does not yet exist — see module docstring.)
    federation = FederationServiceServicer(
        identity, signer, verifier, dispatch_fn=scheduler.admit,
    )

    server = DomainServiceServer(host=host, port=_GRPC_PORT)
    server.add_federation(federation).add_scheduler(scheduler)
    await server.start()

    jwks_provider = JWKSProvider(store)
    http_app = _make_http_app(server, jwks_provider)
    aiorunner = web.AppRunner(http_app)
    await aiorunner.setup()
    site = web.TCPSite(aiorunner, "0.0.0.0", _HTTP_PORT)
    await site.start()

    logger.info(
        "federation.gateway started cluster=%s env=%s grpc=%s:%d http=%d "
        "require_governance=%s mode=admission",
        cluster_id, settings.environment, host, _GRPC_PORT, _HTTP_PORT,
        require_governance,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: no add_signal_handler for these
            pass
    await stop.wait()

    logger.info("federation.gateway shutting down cluster=%s", cluster_id)
    await server.stop()
    await aiorunner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
