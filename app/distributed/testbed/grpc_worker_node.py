"""
Runnable AEOS worker node — one OS process, one gRPC event-bus server.

Used by the cross-process testbed (tests/integration/distributed/
test_grpc_cluster.py) to prove that task dispatch and fail-closed governance
work between *physically separate processes* over real gRPC sockets, not
in-process abstractions.

Run:
    python -m app.distributed.testbed.grpc_worker_node \
        --node-id worker-1 --host 127.0.0.1 --port 50151 \
        --peer 127.0.0.1:50150 --keys-dir /tmp/aeos-keys --environment production

Behavior:
  - Builds a production WorkerRuntime via the standard bootstrap (so
    require_signed_tokens is forced True in the production profile).
  - Registers an "echo" handler that returns the task payload.
  - Prints "READY <address>" on stdout once serving, then runs until it
    receives SIGTERM/SIGINT (or stdin EOF on platforms without signals).
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from app.core.config import AEOSSettings
from app.distributed.contracts.cluster import NodeIdentity
from app.distributed.transport.grpc_bus import GrpcEventBusTransport
from app.distributed.worker.bootstrap import build_worker_runtime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AEOS gRPC worker node")
    p.add_argument("--node-id", required=True)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--peer", action="append", default=[],
                   help="host:port of a peer (repeatable) — typically the scheduler")
    p.add_argument("--keys-dir", required=True)
    p.add_argument("--environment", default="production")
    p.add_argument("--work-ms", type=int, default=20,
                   help="simulated work duration per task")
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    transport = GrpcEventBusTransport(
        args.node_id, host=args.host, port=args.port, peers=list(args.peer)
    )
    settings = AEOSSettings(
        environment=args.environment,
        token_keys_dir=args.keys_dir,
        token_issuer="aeos",
        token_algorithm="ES256",
    )
    await transport.start()

    worker = build_worker_runtime(
        NodeIdentity(node_id=args.node_id, host=args.host, port=transport.bound_port),
        settings=settings,
        transport=transport,
    )

    async def echo_handler(ctx, _):
        if args.work_ms > 0:
            await asyncio.sleep(args.work_ms / 1000.0)
        return {"echoed": ctx.task_payload, "worker": args.node_id}

    worker.register_handler("echo", echo_handler)
    await worker.start()

    # Readiness signal — the testbed polls Ping, but this makes logs legible.
    print(f"READY {transport.address}", flush=True)

    stop = asyncio.Event()

    def _signal(*_a):
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal)
        except (NotImplementedError, ValueError):
            # Windows / non-main-thread: fall back to signal.signal.
            try:
                signal.signal(sig, _signal)
            except (ValueError, OSError):
                pass

    try:
        await stop.wait()
    finally:
        await worker.stop()
        await transport.stop()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
