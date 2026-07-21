"""
Worker process entrypoint — ``python -m app.distributed.worker``.

Closes doc-033 C6 (the worker container crash-loop). The prior
``Dockerfile.worker`` launched ``celery -A app.workers``, but AEOS has no Celery
and no ``app.workers`` module — it uses the ``app.distributed`` runtime. This
module is the real, runnable worker process:

  1. builds a production ``WorkerRuntime`` via the existing
     ``build_worker_runtime`` bootstrap (fail-closed in production);
  2. starts it;
  3. serves the exact probe + metrics surface the Kubernetes manifests already
     expect — ``GET /health`` (liveness/startup), ``GET /health/ready``
     (readiness), ``GET /metrics`` (Prometheus) — on port 9090;
  4. shuts down cleanly on SIGTERM/SIGINT (K8s pod termination).

No new capability is introduced: the runtime, bootstrap, metrics registry, and
Prometheus exporter all pre-exist. This only wires them into a process with the
HTTP endpoints the deployment references.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from aiohttp import web

from app.core.config import settings
from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.metrics.registry import MetricsRegistry
from app.distributed.observability.prometheus import PrometheusExporter
from app.distributed.worker.bootstrap import build_worker_runtime

logger = logging.getLogger(__name__)

_METRICS_PORT = int(os.getenv("WORKER_METRICS_PORT", "9090"))


def _make_health_app(runtime, exporter: PrometheusExporter) -> web.Application:
    """The probe + metrics endpoints the worker manifests reference."""
    app = web.Application()

    async def health(_req: web.Request) -> web.Response:
        # Liveness/startup: the process is up and the runtime object exists.
        return web.json_response({
            "status": "healthy" if runtime.is_running else "starting",
            "node_id": runtime.node_id,
            "environment": settings.environment,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def health_ready(_req: web.Request) -> web.Response:
        # Readiness: the runtime's dispatch loop is actually running and
        # consuming, so the pod can receive traffic/tasks.
        ready = runtime.is_running
        return web.json_response(
            {"ready": ready, "node_id": runtime.node_id},
            status=200 if ready else 503,
        )

    async def metrics(_req: web.Request) -> web.Response:
        return web.Response(
            text=exporter.export(),
            content_type="text/plain",
            charset="utf-8",
        )

    app.router.add_get("/health", health)
    app.router.add_get("/health/ready", health_ready)
    app.router.add_get("/metrics", metrics)
    return app


async def _run() -> None:
    node_id = os.getenv("POD_NAME") or ""
    identity = NodeIdentity.from_env(host=os.getenv("POD_IP", ""), port=0)
    if node_id:
        identity = NodeIdentity(node_id=node_id, host=identity.host, port=0)

    registry = MetricsRegistry(node_id=identity.node_id)
    exporter = PrometheusExporter(registry, node_id=identity.node_id)

    runtime = build_worker_runtime(identity, settings=settings)
    await runtime.start()

    health_app = _make_health_app(runtime, exporter)
    aiorunner = web.AppRunner(health_app)
    await aiorunner.setup()
    site = web.TCPSite(aiorunner, "0.0.0.0", _METRICS_PORT)
    await site.start()
    logger.info(
        "worker.process started node=%s env=%s metrics_port=%d",
        identity.node_id, settings.environment, _METRICS_PORT,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: no add_signal_handler for these
            pass
    await stop.wait()

    logger.info("worker.process shutting down node=%s", identity.node_id)
    await runtime.stop()
    await aiorunner.cleanup()


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()
