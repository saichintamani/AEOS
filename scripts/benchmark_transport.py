#!/usr/bin/env python3
"""
Phase 13 Sprint 2 — Transport benchmark: in-memory vs real gRPC event bus.

Measures the cost of making the AEOS event bus genuinely distributed. Two
transports are compared on the same two workloads:

  1. THROUGHPUT — fire N one-way messages A→B as fast as possible; report
     messages/second measured at the receiver (delivery-complete, not just
     enqueue).
  2. ROUND-TRIP LATENCY — ping/pong: A publishes, B echoes back to A; measure
     wall-clock per round trip and report p50/p95/p99.

InMemoryTransport runs in a single process with no serialization or sockets —
it is the theoretical ceiling. GrpcEventBusTransport runs two real grpc.aio
servers on ephemeral localhost ports and moves every message over a socket with
protobuf framing. The delta between them is the measured price of distribution;
localhost has no network latency, so the gap is pure framing + loopback + event
loop scheduling — a lower bound on real-network overhead, not an estimate of it.

Usage:
    python scripts/benchmark_transport.py
    python scripts/benchmark_transport.py --throughput-count 20000 --latency-count 2000
    python scripts/benchmark_transport.py --json benchmark_results/transport.json

Requires grpcio (skips the gRPC leg with a notice if unavailable).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

from app.distributed.contracts.transport import TransportMessage
from app.distributed.transport.memory import InMemoryTransport

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((p / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _msg(topic: str, payload: bytes) -> TransportMessage:
    return TransportMessage(topic=topic, payload=payload, headers={"schema": "1"})


async def _bench_throughput(pub, sub, *, count: int, warmup: int, window: int) -> dict:
    """Fire `count` messages with a bounded in-flight window (a pipelined
    producer, not a serial one); time until the receiver has all of them."""
    received = 0
    done = asyncio.Event()

    async def handler(_m: TransportMessage) -> None:
        nonlocal received
        received += 1
        if received >= count + warmup:
            done.set()

    await sub.subscribe("bench.throughput", "g", handler)

    payload = b"x" * 256
    sem = asyncio.Semaphore(window)

    async def _one() -> None:
        async with sem:
            await pub.publish(_msg("bench.throughput", payload), wait_for_ack=False)

    # Warmup (JIT stub creation, channel handshake) — not counted.
    await asyncio.gather(*(_one() for _ in range(warmup)))

    start = time.perf_counter()
    producers = [asyncio.create_task(_one()) for _ in range(count)]
    await asyncio.gather(*producers)
    await asyncio.wait_for(done.wait(), timeout=120.0)
    elapsed = time.perf_counter() - start

    return {
        "messages": count,
        "in_flight_window": window,
        "elapsed_s": round(elapsed, 4),
        "throughput_msg_s": round(count / elapsed, 1) if elapsed > 0 else 0.0,
    }


async def _bench_latency(a, b, *, count: int, warmup: int) -> dict:
    """Ping/pong round trips A→B→A; report per-round-trip latency percentiles."""
    pong = asyncio.Event()

    async def echo(_m: TransportMessage) -> None:
        await b.publish(_msg("bench.pong", b"pong"), wait_for_ack=False)

    async def on_pong(_m: TransportMessage) -> None:
        pong.set()

    await b.subscribe("bench.ping", "g", echo)
    await a.subscribe("bench.pong", "g", on_pong)

    samples: list[float] = []
    for i in range(count + warmup):
        pong.clear()
        t0 = time.perf_counter()
        await a.publish(_msg("bench.ping", b"ping"), wait_for_ack=False)
        await asyncio.wait_for(pong.wait(), timeout=10.0)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if i >= warmup:
            samples.append(dt_ms)

    return {
        "round_trips": count,
        "p50_ms": round(_pct(samples, 50), 4),
        "p95_ms": round(_pct(samples, 95), 4),
        "p99_ms": round(_pct(samples, 99), 4),
        "mean_ms": round(statistics.fmean(samples), 4) if samples else 0.0,
    }


async def _run_inmemory(args) -> dict:
    tp = InMemoryTransport()
    await tp.start()
    try:
        thr = await _bench_throughput(tp, tp, count=args.throughput_count, warmup=args.warmup, window=args.window)
        # Fresh instance for latency to avoid handler cross-talk.
        lat_tp = InMemoryTransport()
        await lat_tp.start()
        try:
            lat = await _bench_latency(lat_tp, lat_tp, count=args.latency_count, warmup=args.warmup)
        finally:
            await lat_tp.stop()
        return {"transport": "in-memory", "throughput": thr, "latency": lat}
    finally:
        await tp.stop()


async def _run_grpc(args) -> dict | None:
    try:
        from app.distributed.transport.grpc_bus import GrpcEventBusTransport
    except Exception as exc:  # pragma: no cover - env guard
        print(f"[skip] gRPC transport unavailable: {exc}")
        return None

    a = GrpcEventBusTransport("bench-a", port=0)
    b = GrpcEventBusTransport("bench-b", port=0)
    await a.start()
    await b.start()
    a.add_peer(b.address)
    b.add_peer(a.address)
    try:
        thr = await _bench_throughput(a, b, count=args.throughput_count, warmup=args.warmup, window=args.window)
        lat = await _bench_latency(a, b, count=args.latency_count, warmup=args.warmup)
        return {"transport": "grpc", "throughput": thr, "latency": lat}
    finally:
        await a.stop()
        await b.stop()


def _print_report(results: list[dict]) -> None:
    print("\n" + "=" * 68)
    print("AEOS TRANSPORT BENCHMARK — in-memory vs real gRPC event bus")
    print("=" * 68)
    for r in results:
        thr, lat = r["throughput"], r["latency"]
        print(f"\n[{r['transport']}]")
        print(f"  throughput : {thr['throughput_msg_s']:>12,.1f} msg/s "
              f"({thr['messages']:,} msgs in {thr['elapsed_s']}s)")
        print(f"  latency    : p50={lat['p50_ms']}ms  p95={lat['p95_ms']}ms  "
              f"p99={lat['p99_ms']}ms  mean={lat['mean_ms']}ms "
              f"({lat['round_trips']:,} round trips)")

    mem = next((r for r in results if r["transport"] == "in-memory"), None)
    grpc = next((r for r in results if r["transport"] == "grpc"), None)
    if mem and grpc:
        mt = mem["throughput"]["throughput_msg_s"]
        gt = grpc["throughput"]["throughput_msg_s"]
        ml = mem["latency"]["p50_ms"]
        gl = grpc["latency"]["p50_ms"]
        print("\n[delta — measured price of distribution, localhost]")
        if gt > 0:
            print(f"  throughput : in-memory is {mt / gt:.1f}x the gRPC rate")
        if ml > 0:
            print(f"  latency    : gRPC p50 adds {gl - ml:.3f}ms per round trip "
                  f"(loopback socket + protobuf framing)")
    print("\n" + "=" * 68)


async def _main_async(args) -> int:
    results: list[dict] = []
    results.append(await _run_inmemory(args))
    grpc = await _run_grpc(args)
    if grpc:
        results.append(grpc)

    _print_report(results)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        # Timestamp is supplied by the caller's clock at write time; recorded for
        # provenance only (not used in any measurement).
        payload = {
            "benchmark": "transport",
            "config": {
                "throughput_count": args.throughput_count,
                "latency_count": args.latency_count,
                "warmup": args.warmup,
                "window": args.window,
            },
            "results": results,
        }
        out.write_text(json.dumps(payload, indent=2))
        print(f"\nWrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="AEOS transport benchmark")
    p.add_argument("--throughput-count", type=int, default=10000)
    p.add_argument("--latency-count", type=int, default=1000)
    p.add_argument("--warmup", type=int, default=200)
    p.add_argument("--window", type=int, default=64, help="in-flight publish window for throughput")
    p.add_argument("--json", default=None, help="write raw results JSON to this path")
    args = p.parse_args(argv)
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
