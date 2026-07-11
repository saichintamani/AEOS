"""
AEOS Distributed Execution Engine — Retry Engine

Provides configurable retry policies with:
  - Exponential backoff with jitter
  - Per-attempt timeout
  - Maximum delay cap
  - Circuit breaker (prevents hammering a failing service)
  - Dead-letter tracking

The RetryEngine wraps any async callable (a node executor).
Circuit breakers are per (node_id, resource_key) pair so independent
nodes don't share failure state.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine

from app.core.logger import get_logger
from app.execution.schemas import StepResult, StepStatus

__all__ = [
    "RetryPolicy",
    "CircuitBreakerState",
    "CircuitBreaker",
    "DeadLetterEntry",
    "RetryEngine",
    "DEFAULT_RETRY_POLICY",
    "NO_RETRY_POLICY",
]

log = get_logger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Retry Policy ───────────────────────────────────────────────────────────────

@dataclass
class RetryPolicy:
    """
    Configuration for how a node should be retried on failure.

    Backoff formula: delay = min(base_delay * (factor ** attempt), max_delay)
    With jitter: delay *= uniform(1 - jitter_factor, 1 + jitter_factor)
    """
    max_attempts: int = 3
    base_delay_ms: float = 500.0
    max_delay_ms: float = 30_000.0
    backoff_factor: float = 2.0
    jitter_factor: float = 0.2          # ±20% jitter
    timeout_per_attempt_ms: float = 0.0  # 0 = no per-attempt timeout (use node.timeout_ms)
    retry_on_timeout: bool = True        # whether to retry TIMED_OUT status
    retry_on_failure: bool = True        # whether to retry FAILED status

    def delay_for_attempt(self, attempt: int) -> float:
        """Return delay in seconds for a given attempt number (0-indexed)."""
        delay_ms = min(
            self.base_delay_ms * (self.backoff_factor ** attempt),
            self.max_delay_ms,
        )
        if self.jitter_factor > 0:
            jitter = random.uniform(1.0 - self.jitter_factor, 1.0 + self.jitter_factor)
            delay_ms *= jitter
        return delay_ms / 1000.0

    def should_retry(self, step: StepResult, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if step.status == StepStatus.TIMED_OUT:
            return self.retry_on_timeout
        if step.status == StepStatus.FAILED:
            return self.retry_on_failure
        return False


DEFAULT_RETRY_POLICY = RetryPolicy(max_attempts=3)
NO_RETRY_POLICY = RetryPolicy(max_attempts=1)


# ── Circuit Breaker ────────────────────────────────────────────────────────────

class CircuitBreakerState(str, Enum):
    CLOSED    = "closed"     # Normal operation — requests pass through
    OPEN      = "open"       # Failing fast — no requests pass through
    HALF_OPEN = "half_open"  # Probing — one request allowed to test recovery


@dataclass
class CircuitBreaker:
    """
    Per-node circuit breaker.

    State transitions:
      CLOSED → OPEN when failure_threshold consecutive failures occur
      OPEN → HALF_OPEN after reset_timeout_ms elapses
      HALF_OPEN → CLOSED on success
      HALF_OPEN → OPEN on failure
    """
    failure_threshold: int = 5
    reset_timeout_ms: float = 60_000.0
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    _failure_count: int = field(default=0, repr=False)
    _last_failure_at: float = field(default=0.0, repr=False)  # monotonic seconds

    def is_open(self) -> bool:
        """True if the circuit is open (fast-failing)."""
        if self.state == CircuitBreakerState.OPEN:
            # Check if reset timeout has elapsed
            elapsed_ms = (time.monotonic() - self._last_failure_at) * 1000.0
            if elapsed_ms >= self.reset_timeout_ms:
                self.state = CircuitBreakerState.HALF_OPEN
                log.info(
                    "Circuit breaker half-open (probing)",
                    extra={"ctx_elapsed_ms": round(elapsed_ms, 0)},
                )
                return False
            return True
        return False

    def record_success(self) -> None:
        """Record a successful execution."""
        if self.state == CircuitBreakerState.HALF_OPEN:
            log.info("Circuit breaker closed (recovered)")
        self.state = CircuitBreakerState.CLOSED
        self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed execution; may trip the circuit."""
        self._failure_count += 1
        self._last_failure_at = time.monotonic()
        if self.state == CircuitBreakerState.HALF_OPEN or (
            self.state == CircuitBreakerState.CLOSED
            and self._failure_count >= self.failure_threshold
        ):
            if self.state != CircuitBreakerState.OPEN:
                log.warning(
                    "Circuit breaker opened",
                    extra={
                        "ctx_failure_count": self._failure_count,
                        "ctx_threshold": self.failure_threshold,
                    },
                )
            self.state = CircuitBreakerState.OPEN


# ── Dead Letter ────────────────────────────────────────────────────────────────

@dataclass
class DeadLetterEntry:
    """A node that exhausted all retry attempts."""
    node_id: str
    workflow_id: str
    final_error: str
    attempts: int
    last_status: StepStatus
    dead_lettered_at: str = field(default_factory=_now)
    payload: dict[str, Any] = field(default_factory=dict)


# ── Retry Engine ──────────────────────────────────────────────────────────────

NodeFn = Callable[[], Coroutine[Any, Any, StepResult]]


class RetryEngine:
    """
    Wraps a node executor callable with retry + circuit breaker logic.

    Usage:
        engine = RetryEngine()
        result = await engine.execute_with_retry(
            fn=lambda: my_executor(node, state),
            node_id=node.node_id,
            workflow_id=state.workflow_id,
            policy=RetryPolicy(max_attempts=3),
        )
    """

    def __init__(self) -> None:
        # node_id → CircuitBreaker (shared across workflow executions)
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        # dead letter queue
        self._dead_letters: list[DeadLetterEntry] = []

    async def execute_with_retry(
        self,
        fn: NodeFn,
        node_id: str,
        workflow_id: str = "",
        policy: RetryPolicy | None = None,
        circuit_breaker_key: str | None = None,
    ) -> StepResult:
        """
        Execute fn() with the given retry policy.

        Args:
            fn:                   Async callable returning StepResult
            node_id:              Node ID for logging and dead-letter tracking
            workflow_id:          Workflow ID for dead-letter tracking
            policy:               Retry policy (default: DEFAULT_RETRY_POLICY)
            circuit_breaker_key:  Key for circuit breaker state; defaults to node_id

        Returns:
            StepResult — the last result (success or final failure)
        """
        policy = policy or DEFAULT_RETRY_POLICY
        cb_key = circuit_breaker_key or node_id
        cb = self._circuit_breakers.setdefault(cb_key, CircuitBreaker())

        # Fast-fail: circuit is open
        if cb.is_open():
            log.warning(
                "Circuit breaker open — skipping node execution",
                extra={"ctx_node_id": node_id, "ctx_cb_key": cb_key},
            )
            return StepResult(
                node_id=node_id,
                status=StepStatus.FAILED,
                error=f"Circuit breaker open for key={cb_key!r}. "
                      f"Reset after {cb.reset_timeout_ms:.0f}ms.",
            )

        last_result: StepResult | None = None

        for attempt in range(max(policy.max_attempts, 1)):
            t_start = time.time()
            try:
                result = await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = StepResult(
                    node_id=node_id,
                    status=StepStatus.FAILED,
                    error=str(exc),
                    latency_ms=round((time.time() - t_start) * 1000, 1),
                )

            last_result = result

            if result.status == StepStatus.COMPLETED:
                cb.record_success()
                if attempt > 0:
                    log.info(
                        "Node succeeded after retry",
                        extra={"ctx_node_id": node_id, "ctx_attempt": attempt + 1},
                    )
                return result

            cb.record_failure()

            if not policy.should_retry(result, attempt + 1):
                break

            delay_s = policy.delay_for_attempt(attempt)
            log.info(
                "Retrying node",
                extra={
                    "ctx_node_id": node_id,
                    "ctx_attempt": attempt + 1,
                    "ctx_max_attempts": policy.max_attempts,
                    "ctx_status": result.status.value,
                    "ctx_delay_ms": round(delay_s * 1000, 0),
                    "ctx_error": result.error[:120] if result.error else "",
                },
            )
            await asyncio.sleep(delay_s)

        # All attempts exhausted — dead-letter
        assert last_result is not None
        dl_entry = DeadLetterEntry(
            node_id=node_id,
            workflow_id=workflow_id,
            final_error=last_result.error,
            attempts=policy.max_attempts,
            last_status=last_result.status,
        )
        self._dead_letters.append(dl_entry)
        log.warning(
            "Node dead-lettered after all retry attempts",
            extra={
                "ctx_node_id": node_id,
                "ctx_attempts": policy.max_attempts,
                "ctx_error": last_result.error[:200] if last_result.error else "",
            },
        )
        return last_result

    # ── Circuit Breaker management ─────────────────────────────────────────────

    def reset_circuit(self, key: str) -> None:
        """Manually reset a circuit breaker to CLOSED state."""
        if key in self._circuit_breakers:
            self._circuit_breakers[key].state = CircuitBreakerState.CLOSED
            self._circuit_breakers[key]._failure_count = 0
            log.info("Circuit breaker manually reset", extra={"ctx_key": key})

    def circuit_status(self, key: str) -> CircuitBreakerState | None:
        cb = self._circuit_breakers.get(key)
        return cb.state if cb else None

    # ── Dead letter access ─────────────────────────────────────────────────────

    @property
    def dead_letters(self) -> list[DeadLetterEntry]:
        return list(self._dead_letters)

    def drain_dead_letters(self) -> list[DeadLetterEntry]:
        """Return and clear the dead-letter queue."""
        letters = list(self._dead_letters)
        self._dead_letters.clear()
        return letters

    def summarize(self) -> dict[str, Any]:
        return {
            "circuit_breakers": {
                k: cb.state.value for k, cb in self._circuit_breakers.items()
            },
            "dead_letters_count": len(self._dead_letters),
        }
