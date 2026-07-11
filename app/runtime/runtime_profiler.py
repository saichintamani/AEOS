"""
Wave 9B.4.10 — Runtime Profiler

Captures timing, throughput, and resource metrics for benchmark suites
and production validation.

ProfilerRecord  — one sampled metric
RuntimeProfiler — collects and reports profiling data
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


@dataclass
class ProfilerRecord:
    name: str
    duration_ms: float
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class ProfilerReport:
    records: list[ProfilerRecord] = field(default_factory=list)

    def summary(self, name: str) -> dict:
        subset = [r for r in self.records if r.name == name]
        if not subset:
            return {"name": name, "count": 0}
        durations = sorted(r.duration_ms for r in subset)
        n = len(durations)
        return {
            "name": name,
            "count": n,
            "avg_ms": round(sum(durations) / n, 2),
            "min_ms": round(durations[0], 2),
            "max_ms": round(durations[-1], 2),
            "p50_ms": round(durations[n // 2], 2),
            "p99_ms": round(durations[min(int(n * 0.99), n - 1)], 2),
        }

    def all_summaries(self) -> list[dict]:
        names = {r.name for r in self.records}
        return [self.summary(n) for n in sorted(names)]


class RuntimeProfiler:
    """
    Lightweight profiler that records operation timings.

    Usage:
        async with profiler.measure("decision"):
            decision = await engine.decide(...)
    """

    def __init__(self) -> None:
        self._records: list[ProfilerRecord] = []
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def measure(
        self, name: str, **metadata: Any
    ) -> AsyncIterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            elapsed = (time.monotonic() - start) * 1000.0
            async with self._lock:
                self._records.append(ProfilerRecord(
                    name=name, duration_ms=elapsed, metadata=metadata
                ))

    def record(self, name: str, duration_ms: float, **metadata: Any) -> None:
        self._records.append(ProfilerRecord(
            name=name, duration_ms=duration_ms, metadata=metadata
        ))

    def report(self) -> ProfilerReport:
        return ProfilerReport(records=list(self._records))

    def reset(self) -> None:
        self._records.clear()

    async def async_report(self) -> ProfilerReport:
        async with self._lock:
            return ProfilerReport(records=list(self._records))
