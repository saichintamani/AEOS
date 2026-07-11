"""
Monotonic nanosecond clock implementation.

Uses time.monotonic_ns() with a threading.Lock to guarantee strict monotonicity
within a process even across threads.

Contract: AC-OBS-002
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app.distributed.contracts.coordination import DistributedClock


class MonotonicClock(DistributedClock):
    """
    Process-local monotonic clock.

    Guaranteed to never return the same value twice and never go backwards.
    Resolution is platform-dependent (typically 100ns on Linux, 100ns on macOS).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last: int = 0

    def now_nanos(self) -> int:
        with self._lock:
            ns = time.monotonic_ns()
            if ns <= self._last:
                ns = self._last + 1
            self._last = ns
            return ns

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    @property
    def resolution_ns(self) -> int:
        return 1
