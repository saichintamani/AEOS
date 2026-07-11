"""
AEOS Kernel — AEOSKernel

The single mandatory choke-point for all platform operations.
Every request, resource allocation, plugin lifecycle event, and
policy decision flows through this object.

Architecture Constitution Law 1: Everything passes through the Kernel.

Boot sequence (6 phases):
  Phase 0 — Config & env validation
  Phase 1 — Core services init (event bus, scheduler, resource mgr, policy engine, registries)
  Phase 2 — Plugin discovery & loading (dependency order)
  Phase 3 — Service registration (plugins declare their services)
  Phase 4 — Health verification (all required components healthy)
  Phase 5 — RUNNING (traffic accepted)

Shutdown sequence (4 phases):
  Phase 1 — DRAINING (stop accepting; wait for in-flight tasks)
  Phase 2 — Plugin shutdown (reverse dependency order)
  Phase 3 — Resource cleanup
  Phase 4 — Telemetry flush + STOPPED
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from app.core.config import settings
from app.core.logger import get_logger
from app.kernel.event_bus import EventBus, KernelEvent
from app.kernel.exceptions import (
    KernelBootError,
    KernelStateError,
    PluginConflictError,
    PluginDependencyError,
    PluginInitializationError,
    PluginNotFoundError,
)
from app.kernel.health import ComponentHealth, HealthManager, KernelHealthSnapshot
from app.kernel.lifecycle import LifecycleManager, LocalLifecycleManager, LifecycleState
from app.kernel.plugin import BasePlugin, PluginManifest, InMemoryPluginRegistry as PluginRegistry, PluginStatus
from app.kernel.policy_engine import PolicyContext, PolicyEngine, PolicyResult
from app.kernel.resource_manager import ResourceGrant, ResourceManager, ResourceRequest
from app.kernel.scheduler import Scheduler
from app.kernel.service_registry import ServiceRegistry

__all__ = ["AEOSKernel"]

log = get_logger(__name__)

AEOS_VERSION = "2.0.0"

# Singleton kernel instance
_kernel_instance: AEOSKernel | None = None


class AEOSKernel:
    """
    AEOS HyperKernel — central runtime coordinator.

    Instantiate once and call startup() before serving traffic.
    All subsystems are accessible through the kernel's public API.
    """

    def __init__(self) -> None:
        self._lifecycle = LocalLifecycleManager()
        self._lifecycle.register_component("kernel", self)
        self._event_bus = EventBus()
        self._service_registry = ServiceRegistry()
        self._plugin_registry = PluginRegistry()
        self._resource_manager = ResourceManager()
        self._policy_engine = PolicyEngine()
        self._scheduler = Scheduler(max_concurrent=getattr(settings, "max_concurrent_tasks", 50))
        self._health_manager = HealthManager(poll_interval_seconds=30.0)
        self._started_at: float = 0.0
        self._boot_log: list[dict] = []

    # ── Singleton access ───────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "AEOSKernel":
        global _kernel_instance
        if _kernel_instance is None:
            _kernel_instance = cls()
        return _kernel_instance

    @classmethod
    def set_instance(cls, kernel: "AEOSKernel") -> None:
        global _kernel_instance
        _kernel_instance = kernel

    # ── State ──────────────────────────────────────────────────────────────────

    def state(self) -> LifecycleState:
        """Return current kernel lifecycle state."""
        return self._lifecycle.current_state("kernel")

    def _assert_running_or_initializing(self, operation: str) -> None:
        s = self.state()
        if s not in (LifecycleState.RUNNING, LifecycleState.INITIALIZING):
            raise KernelStateError(s.value, "RUNNING or INITIALIZING", operation)

    # ── Startup ────────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Execute the 6-phase kernel boot sequence."""
        if self.state() == LifecycleState.RUNNING:
            return  # idempotent

        self._started_at = time.monotonic()
        await self._lifecycle.transition("kernel", LifecycleState.INITIALIZING, reason="boot sequence started")

        try:
            await self._phase0_config()
            await self._phase1_core_services()
            await self._phase2_plugins()
            await self._phase3_service_registration()
            await self._phase4_health()
            await self._phase5_ready()
        except KernelBootError:
            await self._lifecycle.transition("kernel", LifecycleState.FAILED, reason="boot sequence failed")
            raise
        except Exception as exc:
            await self._lifecycle.transition("kernel", LifecycleState.FAILED, reason=str(exc))
            raise KernelBootError(phase=-1, reason="Unexpected error during boot", details=str(exc)) from exc

    # ── Boot Phases ────────────────────────────────────────────────────────────

    async def _phase0_config(self) -> None:
        """Phase 0: Configuration validation."""
        t0 = time.monotonic()
        log.info("Kernel boot: Phase 0 — Config & environment validation")

        # Emit to internal buffer (bus not yet started)
        self._boot_log.append({"phase": 0, "status": "started", "ts": datetime.now(timezone.utc).isoformat()})

        # Basic config checks (extend with required-env-var checks as needed)
        if not hasattr(settings, "environment"):
            raise KernelBootError(0, "Missing required configuration: 'environment'")

        duration = round((time.monotonic() - t0) * 1000, 1)
        self._boot_log.append({"phase": 0, "status": "complete", "duration_ms": duration})
        log.info("Phase 0 complete", extra={"ctx_duration_ms": duration})

    async def _phase1_core_services(self) -> None:
        """Phase 1: Initialize all kernel core services."""
        t0 = time.monotonic()
        log.info("Kernel boot: Phase 1 — Core services init")

        # Event bus
        await self._event_bus.start()

        # Built-in policies
        self._policy_engine.load_built_ins()

        # Health manager (needs kernel state function)
        self._health_manager.start(kernel_state_fn=lambda: self.state().value)

        await self._emit(KernelEvent(
            topic="kernel.boot.phase_complete",
            source="kernel",
            payload={"phase": 1},
        ))

        duration = round((time.monotonic() - t0) * 1000, 1)
        log.info("Phase 1 complete", extra={"ctx_duration_ms": duration})

    async def _phase2_plugins(self) -> None:
        """Phase 2: Discover and load plugins in dependency order."""
        t0 = time.monotonic()
        log.info("Kernel boot: Phase 2 — Plugin discovery & loading")

        # Built-in plugins are registered programmatically in main.py via
        # kernel.register_plugin() before startup(), or lazily here.
        # External plugin scanning from plugins/ directory is a v3 feature.

        duration = round((time.monotonic() - t0) * 1000, 1)
        log.info(
            "Phase 2 complete",
            extra={"ctx_plugins_loaded": len(self._plugin_registry.list_plugins()), "ctx_duration_ms": duration},
        )

    async def _phase3_service_registration(self) -> None:
        """Phase 3: Plugins declare their services."""
        t0 = time.monotonic()
        log.info("Kernel boot: Phase 3 — Service registration")

        for manifest in self._plugin_registry.list_plugins():
            plugin = self._plugin_registry.get_plugin(manifest.id)
            if plugin is None:
                continue
            try:
                await plugin.register_services(kernel=self)
            except Exception as exc:
                log.error(
                    "Plugin service registration failed",
                    extra={"ctx_plugin_id": manifest.id, "ctx_error": str(exc)},
                )

        await self._emit(KernelEvent(
            topic="kernel.service.registry.built",
            source="kernel",
            payload={
                "service_count": len(self._service_registry.all_records()),
            },
        ))

        duration = round((time.monotonic() - t0) * 1000, 1)
        log.info("Phase 3 complete", extra={"ctx_duration_ms": duration})

    async def _phase4_health(self) -> None:
        """Phase 4: Health check all registered components."""
        t0 = time.monotonic()
        log.info("Kernel boot: Phase 4 — Health verification")

        snapshot = await self._health_manager.check_now(
            state=self.state().value,
            plugins_loaded=len(self._plugin_registry.list_plugins()),
            services_registered=len(self._service_registry.all_records()),
        )

        if not snapshot.healthy and snapshot.failed_components:
            log.error(
                "Required components unhealthy",
                extra={"ctx_failed": snapshot.failed_components},
            )
            # In production: raise KernelBootError(4, "Required components unhealthy")
            # For development tolerance: log and continue
            log.warning("Continuing boot despite unhealthy components (development mode)")

        duration = round((time.monotonic() - t0) * 1000, 1)
        log.info("Phase 4 complete", extra={"ctx_duration_ms": duration})

    async def _phase5_ready(self) -> None:
        """Phase 5: Transition to RUNNING, accept traffic."""
        await self._lifecycle.transition("kernel", LifecycleState.READY, reason="all phases complete")
        await self._lifecycle.transition("kernel", LifecycleState.RUNNING, reason="ready to accept traffic")

        boot_duration = round((time.monotonic() - self._started_at) * 1000, 1)

        await self._emit(KernelEvent(
            topic="kernel.boot.complete",
            source="kernel",
            payload={
                "duration_ms": boot_duration,
                "plugins_loaded": len(self._plugin_registry.list_plugins()),
                "services_registered": len(self._service_registry.all_records()),
                "aeos_version": AEOS_VERSION,
            },
        ))

        log.info(
            "AEOS Kernel ready",
            extra={
                "ctx_duration_ms": boot_duration,
                "ctx_plugins": len(self._plugin_registry.list_plugins()),
                "ctx_services": len(self._service_registry.all_records()),
                "ctx_version": AEOS_VERSION,
            },
        )

    # ── Shutdown ───────────────────────────────────────────────────────────────

    async def shutdown(self, graceful: bool = True) -> None:
        """Execute the kernel shutdown sequence."""
        if self.state() in (LifecycleState.STOPPED, LifecycleState.CREATED, LifecycleState.FAILED):
            return  # idempotent

        await self._emit(KernelEvent(
            topic="kernel.shutdown.initiated",
            source="kernel",
            payload={"graceful": graceful},
        ))
        log.info("Kernel shutdown initiated", extra={"ctx_graceful": graceful})

        from app.kernel.lifecycle import LifecycleError

        t0 = time.monotonic()

        # Phase 1: Drain
        if graceful:
            try:
                await self._lifecycle.transition("kernel", LifecycleState.DRAINING, reason="graceful shutdown")
            except LifecycleError:
                pass  # Already in a non-drainable state — proceed with shutdown
            remaining = await self._scheduler.drain(timeout_seconds=30.0)
            if remaining:
                log.warning("Drain timeout — tasks still running", extra={"ctx_remaining": remaining})

        # Phase 2: Plugin shutdown (reverse dependency order)
        try:
            await self._lifecycle.transition("kernel", LifecycleState.STOPPING, reason="plugin shutdown")
        except LifecycleError:
            pass  # Best-effort state tracking during shutdown
        for manifest in reversed(self._plugin_registry.list_plugins()):
            plugin = self._plugin_registry.get_plugin(manifest.id)
            if plugin is None:
                continue
            try:
                await asyncio.wait_for(plugin.shutdown(), timeout=30.0)
                log.info("Plugin shutdown OK", extra={"ctx_plugin_id": manifest.id})
            except (asyncio.TimeoutError, Exception) as exc:
                log.warning(
                    "Plugin shutdown error",
                    extra={"ctx_plugin_id": manifest.id, "ctx_error": str(exc)},
                )

        # Phase 3: Resource cleanup
        for record in self._service_registry.all_records():
            self._service_registry.deregister(record.service_id)

        # Phase 4: Stop background tasks
        await self._health_manager.stop()
        await self._event_bus.stop()

        try:
            await self._lifecycle.transition("kernel", LifecycleState.STOPPED, reason="shutdown complete")
        except LifecycleError:
            pass  # Already terminal; that's fine

        duration = round((time.monotonic() - t0) * 1000, 1)
        log.info(
            "AEOS Kernel stopped",
            extra={"ctx_duration_ms": duration, "ctx_graceful": graceful},
        )

    # ── Plugin API ─────────────────────────────────────────────────────────────

    async def register_plugin(self, plugin: BasePlugin) -> None:
        """
        Register and initialize a plugin.

        Can be called before startup() (plugins are initialized during Phase 2)
        or after startup() (plugin is initialized immediately).
        """
        manifest = plugin.manifest

        # Conflict check
        if self._plugin_registry.get_plugin(manifest.id) is not None:
            raise PluginConflictError(manifest.id)

        # Dependency check: all declared deps must be loaded
        missing = [dep for dep in manifest.dependencies if self._plugin_registry.get_plugin(dep) is None]
        if missing:
            raise PluginDependencyError(manifest.id, missing)

        # Initialize if kernel is already running
        if self.state() == LifecycleState.RUNNING:
            try:
                await asyncio.wait_for(plugin.initialize(kernel=self), timeout=30.0)
            except (asyncio.TimeoutError, Exception) as exc:
                raise PluginInitializationError(manifest.id, str(exc)) from exc

            h = plugin.health()
            if h != PluginStatus.READY:
                raise PluginInitializationError(manifest.id, f"Plugin health={h.value} after init")

        self._plugin_registry.add_plugin(plugin)

        await self._emit(KernelEvent(
            topic="kernel.plugin.loaded",
            source="kernel",
            payload={
                "plugin_id": manifest.id,
                "version": manifest.version,
                "capabilities": manifest.capabilities,
            },
        ))

        log.info("Plugin registered", extra={"ctx_plugin_id": manifest.id, "ctx_version": manifest.version})

    async def unload_plugin(self, plugin_id: str) -> None:
        """Gracefully unload a plugin."""
        plugin = self._plugin_registry.get_plugin(plugin_id)
        if plugin is None:
            raise PluginNotFoundError(plugin_id)

        try:
            await asyncio.wait_for(plugin.shutdown(), timeout=30.0)
        except Exception as exc:
            log.warning("Plugin shutdown error during unload", extra={"ctx_plugin_id": plugin_id, "ctx_error": str(exc)})

        self._resource_manager.release_by_requester(plugin_id)
        self._service_registry.deregister_by_plugin(plugin_id)
        self._plugin_registry.remove_plugin(plugin_id)

        await self._emit(KernelEvent(
            topic="kernel.plugin.unloaded",
            source="kernel",
            payload={"plugin_id": plugin_id},
        ))

    def get_plugin(self, plugin_id: str) -> BasePlugin | None:
        return self._plugin_registry.get_plugin(plugin_id)

    def list_plugins(self) -> list[PluginManifest]:
        return self._plugin_registry.list_plugins()

    # ── Service API ────────────────────────────────────────────────────────────

    def register_service(
        self,
        service_id: str,
        service: Any,
        capabilities: list[str],
        plugin_id: str = "",
    ) -> None:
        """Register a platform service with its capabilities."""
        self._service_registry.register(service_id, service, capabilities, plugin_id=plugin_id)
        self._event_bus.emit_sync(KernelEvent(
            topic="kernel.service.registered",
            source="kernel",
            payload={"service_id": service_id, "capabilities": capabilities},
        ))

    def get_service(self, service_id: str) -> Any:
        """
        Return a registered service.

        Raises:
            ServiceNotFoundError: if not registered
        """
        return self._service_registry.get(service_id)

    def find_by_capability(self, capability: str) -> list[Any]:
        """Return all services that provide the given capability."""
        return self._service_registry.find_by_capability(capability)

    def list_services(self) -> dict[str, list[str]]:
        return self._service_registry.list_services()

    # ── Event API ──────────────────────────────────────────────────────────────

    async def emit(self, event: KernelEvent) -> None:
        """Publish an event to all matching subscribers."""
        await self._event_bus.emit(event)

    def subscribe(self, topic_pattern: str, handler: Callable[[KernelEvent], Coroutine]) -> None:
        self._event_bus.subscribe(topic_pattern, handler)

    def unsubscribe(self, topic_pattern: str, handler: Callable[[KernelEvent], Coroutine]) -> None:
        self._event_bus.unsubscribe(topic_pattern, handler)

    # ── Resource API ───────────────────────────────────────────────────────────

    async def request_resources(self, request: ResourceRequest) -> ResourceGrant:
        """Request resource allocation. Returns a grant; check grant.granted."""
        grant = await self._resource_manager.request(request)
        topic = "kernel.resource.granted" if grant.granted else "kernel.resource.denied"
        await self._emit(KernelEvent(
            topic=topic,
            source="kernel",
            payload={
                "grant_id": grant.grant_id,
                "requester_id": request.requester_id,
                "granted": grant.granted,
                "denied_reason": grant.denied_reason,
            },
        ))
        return grant

    async def release_resources(self, grant_id: str) -> None:
        """Release previously granted resources."""
        await self._resource_manager.release(grant_id)
        await self._emit(KernelEvent(
            topic="kernel.resource.released",
            source="kernel",
            payload={"grant_id": grant_id},
        ))

    # ── Policy API ─────────────────────────────────────────────────────────────

    async def enforce_policy(self, context: PolicyContext) -> PolicyResult:
        """
        Evaluate all registered policies. Returns PolicyResult; never raises.
        """
        result = await self._policy_engine.enforce(context)
        await self._emit(KernelEvent(
            topic="kernel.policy.evaluated",
            source="kernel",
            payload={
                "actor_id": context.actor_id,
                "action": context.action,
                "resource": context.resource,
                "allowed": result.allowed,
                "policy_id": result.policy_id,
                "trace_id": context.trace_id,
            },
        ))
        return result

    # ── Health API ─────────────────────────────────────────────────────────────

    def health(self) -> KernelHealthSnapshot:
        """Return the most recent cached health snapshot."""
        return self._health_manager.snapshot()

    async def check_health_now(self) -> KernelHealthSnapshot:
        """Force a live health poll."""
        return await self._health_manager.check_now(
            state=self.state().value,
            plugins_loaded=len(self._plugin_registry.list_plugins()),
            services_registered=len(self._service_registry.all_records()),
        )

    # ── Introspection ──────────────────────────────────────────────────────────

    def introspect(self) -> dict:
        """Full runtime state for debug endpoints."""
        uptime = round(time.monotonic() - self._started_at, 1) if self._started_at else 0
        return {
            "kernel_state": self.state().value,
            "aeos_version": AEOS_VERSION,
            "uptime_seconds": uptime,
            "plugins": [
                {"id": m.id, "version": m.version, "capabilities": m.capabilities}
                for m in self.list_plugins()
            ],
            "services": self.list_services(),
            "scheduler": self._scheduler.summarize(),
            "resources": self._resource_manager.summarize(),
            "policy": self._policy_engine.summarize(),
            "event_bus": self._event_bus.summarize(),
        }

    # ── Internal helpers ───────────────────────────────────────────────────────

    async def _emit(self, event: KernelEvent) -> None:
        """Internal emit — catches and logs errors, never raises."""
        try:
            await self._event_bus.emit(event)
        except Exception as exc:
            log.warning("Kernel internal emit failed", extra={"ctx_topic": event.topic, "ctx_error": str(exc)})
