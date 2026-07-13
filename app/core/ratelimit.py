"""
AEOS — Rate limiting primitives (shared across the whole API surface)

A dependency-free, thread-safe token-bucket limiter with optional exponential
backoff for repeat offenders. Lives in `core` so any layer may depend on it
(rag, api, kernel) without a layering violation.

Tiers are configured centrally in app/core/config.py and wired in app/main.py:
  - "auth"      strictest  (per-IP + per-account, exponential backoff)
  - "expensive" strict     (agent runs, training, repo indexing)
  - "rag"       moderate    (ingest / query / answer / upload)
  - "default"   loose       (everything else / read endpoints)

Nothing here is hardcoded to a threshold — callers pass capacity/refill/backoff.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class RateDecision:
    allowed: bool
    retry_after: float  # seconds until the caller may retry (0 when allowed)


class RateLimiter:
    """
    Token-bucket rate limiter keyed by an arbitrary string (typically client IP,
    or "ip:account" for per-account auth limits).

    `capacity` tokens refill at `refill_per_sec` (default capacity/60 → ~capacity
    requests per minute, with bursts up to capacity).

    Optional exponential backoff: when `penalty_base` > 0, each *consecutive*
    denial for a key blocks it for base, 2·base, 4·base … up to `penalty_max`
    seconds. A successful request resets the penalty. This replaces hard lockouts
    with a self-healing backoff, which is the recommended pattern for auth routes.
    """

    def __init__(
        self,
        capacity: int = 60,
        refill_per_sec: float | None = None,
        penalty_base: float = 0.0,
        penalty_max: float = 300.0,
    ) -> None:
        self._capacity = float(max(1, capacity))
        self._refill = refill_per_sec if refill_per_sec is not None else self._capacity / 60.0
        self._penalty_base = float(penalty_base)
        self._penalty_max = float(penalty_max)
        # key -> [tokens, last_ts, blocked_until, consecutive_denials]
        self._buckets: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> RateDecision:
        """Atomically test-and-consume one token for `key`."""
        t = now if now is not None else time.monotonic()
        with self._lock:
            tokens, last, blocked_until, denials = self._buckets.get(key, [self._capacity, t, 0.0, 0.0])

            # Still inside an active backoff window?
            if blocked_until > t:
                self._buckets[key] = [tokens, last, blocked_until, denials]
                return RateDecision(False, round(blocked_until - t, 3))

            # Refill.
            tokens = min(self._capacity, tokens + (t - last) * self._refill)
            last = t

            if tokens < 1.0:
                # Denied → apply/extend exponential backoff if configured.
                denials += 1
                if self._penalty_base > 0.0:
                    block = min(self._penalty_max, self._penalty_base * (2 ** (denials - 1)))
                    blocked_until = t + block
                    retry = block
                else:
                    retry = max(0.0, (1.0 - tokens) / self._refill)
                self._buckets[key] = [tokens, last, blocked_until, denials]
                return RateDecision(False, round(retry, 3))

            # Allowed → consume and reset penalty state.
            self._buckets[key] = [tokens - 1.0, last, 0.0, 0.0]
            return RateDecision(True, 0.0)

    def allow(self, key: str) -> bool:
        """Backward-compatible boolean check (ignores retry-after)."""
        return self.check(key).allowed
