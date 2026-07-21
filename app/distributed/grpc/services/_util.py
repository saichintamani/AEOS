"""
Shared helpers for the AEOS domain gRPC servicers.

Kept deliberately tiny: proto Timestamp/Duration conversion and a broadcast
fan-out primitive used by the streaming RPCs (WatchGovernanceEvents,
WatchTask, WatchEvents, WatchFederationEvents). The servicers themselves hold
the domain logic and delegate to the real backing classes (TokenSigner /
TokenVerifier / WorkerPool / …).

Phase: 13 Sprint 3
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from google.protobuf.duration_pb2 import Duration
from google.protobuf.timestamp_pb2 import Timestamp


def now_ts() -> Timestamp:
    """A Timestamp pinned to the wall clock at call time."""
    ts = Timestamp()
    ts.FromNanoseconds(int(time.time() * 1_000_000_000))
    return ts


def ts_from_epoch(seconds: float) -> Timestamp:
    ts = Timestamp()
    ts.FromNanoseconds(int(seconds * 1_000_000_000))
    return ts


def duration_from_seconds(seconds: float) -> Duration:
    d = Duration()
    d.FromNanoseconds(int(seconds * 1_000_000_000))
    return d


class Broadcaster:
    """Fan-out hub for server-streaming RPCs.

    Each live stream subscribes with a bounded asyncio.Queue; publish() puts a
    copy on every subscriber's queue (dropping on a full/slow consumer rather
    than blocking the producer). This is the same "broadcast to interested
    consumers" shape the event bus uses, scoped to one process.
    """

    def __init__(self, maxsize: int = 1024) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        async with self._lock:
            self._subs.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue) -> None:
        async with self._lock:
            self._subs.discard(q)

    async def publish(self, item: Any) -> None:
        async with self._lock:
            targets = list(self._subs)
        for q in targets:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer guard
                pass
