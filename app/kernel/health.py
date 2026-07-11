"""
AEOS Kernel — Health Manager

Tracks and aggregates the health state of all kernel-managed components
(plugins, services, and the kernel itself).

Health is polled in the background at a configurable interval (default 30s).
The health() method returns the last cached snapshot — it does not trigger
a live poll. Use check_now() for forced live checks (e.g., /health endpoint).

Health levels:
  HEALTHY   — all required components OK
  DEGRADED  — optional components unhealthy; platform still functional
  UNHEALTHY — one or more required components failed
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from app.core.logger import get_logger

__all__ = [
    "ComponentHealth",
    "KernelHealthSnapshot",
    "HealthManager",
]

log = get_logger(__name__)


class ComponentHealth(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"


@dataclass
class ComponentStatus:
    component_id: str
    health: ComponentHealth = ComponentHealth.UNKNOWN
    required: bool = True
    last_checked_at: str = ""
    error: str = ""


@dataclass
class KernelHealthSnapshot:
    """Point-in-time kernel health report."""
    healthy: bool
    state: str
    components: list[ComponentStatus] = field(default_factory=list)
    plugins_loaded: int = 0
    services_registered: int = 0
    uptime_seconds: float = 0.0
    failed_components: list[str] = field(default_factory=list)
    snapshot_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class HealthManager:
    """
    Background health aggregator for all kernel components.

    Components register a health probe (any callable returning ComponentHealth).
    The manager polls all probes at the configured interval and caches the result.
    """

    def __init__(self, poll_interval_seconds: float = 30.0) -> None:
        self._poll_interval = poll_interval_seconds
        self._components: dict[str, tuple[bool, Callable[[], ComponentHealth]]] = {}
        self._last_snapshot: KernelHealthSnapshot | None = None
        self._poll_task: asyncio.Task | None = None
        self._started_at: float = 0.0
        self._kernel_state_fn: Callable[[], str] = lambda: "unknown"

    # ── Registration ───────────────────────────────────────────────────────────

    def register(
        self,
        component_id: str,
        probe: Callable[[], ComponentHealth],
        required: bool = True,
    ) -> None:
        """Register a component health probe."""
        self._components[component_id] = (required, probe)

    def unregister(self, component_id: str) -> None:
        self._components.pop(component_id, None)

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self, kernel_state_fn: Callable[[], str]) -> None:
        """Start background polling. kernel_state_fn returns current kernel LifecycleState name."""
        self._kernel_state_fn = kernel_state_fn
        self._started_at = time.monotonic()
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name="health-manager-poll",
        )

    async def stop(self) -> None:
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass

    # ── Snapshot ───────────────────────────────────────────────────────────────

    def snapshot(self) -> KernelHealthSnapshot:
        """Return the last cached health snapshot."""
        if self._last_snapshot is None:
            return KernelHealthSnapshot(
                healthy=False,
                state="unknown",
                failed_components=[],
            )
        return self._last_snapshot

    async def check_now(
        self,
        state: str = "unknown",
        plugins_loaded: int = 0,
        services_registered: int = 0,
    ) -> KernelHealthSnapshot:
        """Force an immediate health poll and update the cached snapshot."""
        statuses: list[ComponentStatus] = []
        failed: list[str] = []
        overall_healthy = True

        for comp_id, (required, probe) in list(self._components.items()):
            try:
                if asyncio.iscoroutinefunction(probe):
                    h = await asyncio.wait_for(probe(), timeout=10.0)
                else:
                    h = await asyncio.wait_for(asyncio.to_thread(probe), timeout=10.0)
            except asyncio.TimeoutError:
                h = ComponentHealth.UNHEALTHY
                err = "health check timeout"
            except Exception as exc:
                h = ComponentHealth.UNHEALTHY
                err = str(exc)
            else:
                err = ""

            status = ComponentStatus(
                component_id=comp_id,
                health=h,
                required=required,
                last_checked_at=datetime.now(timezone.utc).isoformat(),
                error=err,
            )
            statuses.append(status)

            if h == ComponentHealth.UNHEALTHY and required:
                overall_healthy = False
                failed.append(comp_id)

        uptime = time.monotonic() - self._started_at if self._started_at else 0.0
        snapshot = KernelHealthSnapshot(
            healthy=overall_healthy,
            state=state,
            components=statuses,
            plugins_loaded=plugins_loaded,
            services_registered=services_registered,
            uptime_seconds=round(uptime, 1),
            failed_components=failed,
        )
        self._last_snapshot = snapshot
        return snapshot

    # ── Background poll ────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._poll_interval)
                state = self._kernel_state_fn()
                await self.check_now(state=state)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.warning("Health poll error", extra={"ctx_error": str(exc)})
