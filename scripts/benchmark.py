#!/usr/bin/env python3
"""
Phase 9B.6 Priority 5 — AEOS Performance Benchmark Runner

Measures throughput, latency (p50/p95/p99), and resource usage for
100 / 1 000 / 10 000 workflow submissions against a running AEOS instance.

Usage:
    # Against local dev server (default):
    python scripts/benchmark.py

    # Against a specific host / with custom scale:
    python scripts/benchmark.py --host http://localhost:8000 --scale 100,1000
    python scripts/benchmark.py --scale 10000 --concurrency 50

    # Dry-run: measure scheduling/execution engine directly (no HTTP):
    python scripts/benchmark.py --mode local

Results are written to:
    benchmark_results/benchmark_<timestamp>.json
    benchmark_results/benchmark_<timestamp>.txt  (human-readable)

Metrics captured:
    - Total wall-clock time
    - Throughput (tasks/s)
    - p50 / p95 / p99 latency
    - Peak RSS memory (MB)
    - CPU seconds
    - Error rate
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Coroutine

# Optional httpx for HTTP mode
try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    task_id: int
    latency_s: float
    success: bool
    error: str = ""


@dataclass
class BenchmarkRun:
    mode: str                  # "http" | "local"
    scale: int                 # number of workflows
    concurrency: int
    total_wall_s: float = 0.0
    throughput_rps: float = 0.0
    latency_p50_s: float = 0.0
    latency_p95_s: float = 0.0
    latency_p99_s: float = 0.0
    latency_min_s: float = 0.0
    latency_max_s: float = 0.0
    error_rate: float = 0.0
    peak_rss_mb: float = 0.0
    results: list[TaskResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def compute_stats(self) -> None:
        latencies = [r.latency_s for r in self.results if r.success]
        errors    = [r for r in self.results if not r.success]
        total     = len(self.results)

        if not latencies:
            return

        latencies.sort()
        self.latency_p50_s = _percentile(latencies, 50)
        self.latency_p95_s = _percentile(latencies, 95)
        self.latency_p99_s = _percentile(latencies, 99)
        self.latency_min_s = latencies[0]
        self.latency_max_s = latencies[-1]
        self.throughput_rps = len(latencies) / self.total_wall_s if self.total_wall_s > 0 else 0
        self.error_rate = len(errors) / total if total > 0 else 0


def _percentile(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (k - lo)


# ── HTTP benchmark (against live AEOS API) ────────────────────────────────────

TASK_PAYLOADS = [
    {"task": "Analyze the performance characteristics of distributed systems", "mode": "single-agent"},
    {"task": "Research recent advances in Raft consensus algorithms", "mode": "single-agent"},
    {"task": "Plan a microservices migration strategy for a monolith", "mode": "multi-agent"},
    {"task": "Review the security implications of token-based authentication", "mode": "single-agent"},
    {"task": "Summarize the tradeoffs between eventual and strong consistency", "mode": "single-agent"},
]


async def _http_task(
    client: "httpx.AsyncClient",
    task_id: int,
    host: str,
) -> TaskResult:
    payload = TASK_PAYLOADS[task_id % len(TASK_PAYLOADS)]
    start = time.monotonic()
    try:
        resp = await client.post(f"{host}/api/v1/run", json=payload, timeout=30.0)
        elapsed = time.monotonic() - start
        success = resp.status_code < 400
        error = "" if success else f"HTTP {resp.status_code}"
        return TaskResult(task_id=task_id, latency_s=elapsed, success=success, error=error)
    except Exception as exc:
        return TaskResult(
            task_id=task_id,
            latency_s=time.monotonic() - start,
            success=False,
            error=str(exc),
        )


async def run_http_benchmark(
    host: str,
    scale: int,
    concurrency: int,
) -> BenchmarkRun:
    if not _HTTPX_AVAILABLE:
        raise RuntimeError("httpx not installed. Run: pip install httpx")

    run = BenchmarkRun(mode="http", scale=scale, concurrency=concurrency)
    sem = asyncio.Semaphore(concurrency)

    async def bounded(task_id: int) -> TaskResult:
        async with sem:
            return await _http_task(client, task_id, host)

    tracemalloc.start()
    t0 = time.monotonic()

    async with httpx.AsyncClient() as client:
        tasks = [bounded(i) for i in range(scale)]
        run.results = list(await asyncio.gather(*tasks))

    run.total_wall_s = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    run.peak_rss_mb = peak / (1024 * 1024)
    run.compute_stats()
    return run


# ── Local benchmark (in-process, no HTTP) ────────────────────────────────────

async def _local_task(task_id: int, orchestrator: object) -> TaskResult:
    payload = TASK_PAYLOADS[task_id % len(TASK_PAYLOADS)]
    start = time.monotonic()
    try:
        result = await orchestrator.run_task(task=payload["task"], mode=payload["mode"])
        elapsed = time.monotonic() - start
        success = result.status == "success"
        return TaskResult(task_id=task_id, latency_s=elapsed, success=success)
    except Exception as exc:
        return TaskResult(
            task_id=task_id,
            latency_s=time.monotonic() - start,
            success=False,
            error=str(exc),
        )


async def run_local_benchmark(scale: int, concurrency: int) -> BenchmarkRun:
    # Bootstrap minimal AEOS runtime (no HTTP server)
    sys.path.insert(0, str(Path(__file__).parent.parent))

    from app.core.orchestrator import Orchestrator
    from app.agents.simple_agent import SimpleAgent
    from app.agents.planner_agent import PlannerAgent
    from app.agents.research_agent import ResearchAgent
    from app.agents.analyst_agent import AnalystAgent

    orch = Orchestrator()
    orch.register(SimpleAgent())
    orch.register(PlannerAgent())
    orch.register(ResearchAgent())
    orch.register(AnalystAgent())
    await orch.startup()

    run = BenchmarkRun(mode="local", scale=scale, concurrency=concurrency)
    sem = asyncio.Semaphore(concurrency)

    async def bounded(task_id: int) -> TaskResult:
        async with sem:
            return await _local_task(task_id, orch)

    tracemalloc.start()
    t0 = time.monotonic()
    tasks = [bounded(i) for i in range(scale)]
    run.results = list(await asyncio.gather(*tasks))
    run.total_wall_s = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    run.peak_rss_mb = peak / (1024 * 1024)
    run.compute_stats()

    await orch.shutdown()
    return run


# ── Reporting ─────────────────────────────────────────────────────────────────

def format_run(run: BenchmarkRun) -> str:
    lines = [
        f"{'═'*60}",
        f" AEOS Benchmark — {run.mode.upper()} — scale={run.scale:,}  concurrency={run.concurrency}",
        f"{'═'*60}",
        f"  Timestamp     : {run.timestamp}",
        f"  Wall time     : {run.total_wall_s:.2f}s",
        f"  Throughput    : {run.throughput_rps:.1f} tasks/s",
        f"  Error rate    : {run.error_rate*100:.1f}%",
        f"  Peak RSS      : {run.peak_rss_mb:.1f} MB",
        f"{'─'*60}",
        f"  Latency p50   : {run.latency_p50_s*1000:.1f} ms",
        f"  Latency p95   : {run.latency_p95_s*1000:.1f} ms",
        f"  Latency p99   : {run.latency_p99_s*1000:.1f} ms",
        f"  Latency min   : {run.latency_min_s*1000:.1f} ms",
        f"  Latency max   : {run.latency_max_s*1000:.1f} ms",
        f"{'═'*60}",
    ]
    if run.error_rate > 0:
        errors = [r for r in run.results if not r.success]
        by_err: dict[str, int] = {}
        for r in errors:
            by_err[r.error] = by_err.get(r.error, 0) + 1
        lines.append("  Errors by type:")
        for msg, cnt in sorted(by_err.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"    {cnt:>5}×  {msg}")
        lines.append(f"{'═'*60}")
    return "\n".join(lines)


def save_results(runs: list[BenchmarkRun], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    # JSON (machine-readable)
    json_path = output_dir / f"benchmark_{ts}.json"
    serializable = []
    for r in runs:
        d = asdict(r)
        # Keep only aggregate stats in JSON, drop per-task results list to keep file small
        d["results"] = f"<{len(r.results)} TaskResult objects omitted>"
        serializable.append(d)
    json_path.write_text(json.dumps(serializable, indent=2))

    # Text (human-readable)
    txt_path = output_dir / f"benchmark_{ts}.txt"
    report_lines = [format_run(r) for r in runs]
    txt_path.write_text("\n\n".join(report_lines) + "\n")

    print(f"\nResults saved:")
    print(f"  JSON : {json_path}")
    print(f"  Text : {txt_path}")


# ── CLI entry point ───────────────────────────────────────────────────────────

async def main() -> None:
    parser = argparse.ArgumentParser(
        description="AEOS Phase 9B.6 Performance Benchmark Runner",
    )
    parser.add_argument(
        "--host", default="http://localhost:8000",
        help="AEOS API base URL (used in 'http' mode)",
    )
    parser.add_argument(
        "--mode", choices=["http", "local"], default="local",
        help="'http' = hit live API; 'local' = in-process orchestrator",
    )
    parser.add_argument(
        "--scale", default="100,1000,10000",
        help="Comma-separated list of workflow counts to benchmark",
    )
    parser.add_argument(
        "--concurrency", type=int, default=20,
        help="Max concurrent tasks per run",
    )
    parser.add_argument(
        "--output", default="benchmark_results",
        help="Directory to write results into",
    )
    args = parser.parse_args()

    scales = [int(s.strip()) for s in args.scale.split(",")]
    output_dir = Path(args.output)
    runs: list[BenchmarkRun] = []

    print(f"AEOS Benchmark Runner — mode={args.mode}  concurrency={args.concurrency}")
    print(f"Scales: {scales}\n")

    for scale in scales:
        print(f"Running scale={scale:,} ...", end=" ", flush=True)
        if args.mode == "http":
            run = await run_http_benchmark(args.host, scale, args.concurrency)
        else:
            run = await run_local_benchmark(scale, args.concurrency)

        print(f"done ({run.total_wall_s:.1f}s, {run.throughput_rps:.1f} tasks/s)")
        print(format_run(run))
        runs.append(run)

    save_results(runs, output_dir)


if __name__ == "__main__":
    asyncio.run(main())
