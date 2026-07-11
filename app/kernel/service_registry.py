"""
AEOS Kernel — Service Registry

Maintains the canonical map of platform services: what services exist,
what capabilities they provide, and how to locate them.

Rules:
  - Service IDs are unique (snake_case noun phrases)
  - Capabilities are dot-namespaced strings: "domain.action"
  - Registration is idempotent for the same (id, instance) pair
  - Discovery is O(1) by service_id, O(n_services) by capability
  - No service may be registered while the kernel is STOPPED or FAILED
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.core.logger import get_logger
from app.kernel.exceptions import (
    ServiceConflictError,
    ServiceNotFoundError,
    InvalidCapabilityError,
)

__all__ = [
    "ServiceHealth",
    "ServiceRecord",
    "ServiceRegistry",
]

log = get_logger(__name__)

# Capability string: must be "word.word" or "word.word.word" etc.
_CAP_SEPARATORS = {"."}


def _validate_capability(cap: str) -> None:
    if not cap or "." not in cap:
        raise InvalidCapabilityError(cap, "must contain at least one dot separator (e.g. 'rag.query')")
    if cap.startswith(".") or cap.endswith("."):
        raise InvalidCapabilityError(cap, "must not start or end with a dot")
    parts = cap.split(".")
    if any(not p or not p.replace("_", "").isalnum() for p in parts):
        raise InvalidCapabilityError(cap, "each segment must be non-empty alphanumeric/underscore")


class ServiceHealth(str, Enum):
    HEALTHY   = "healthy"
    DEGRADED  = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN   = "unknown"


@dataclass
class ServiceRecord:
    """Everything the registry knows about a registered service."""
    service_id: str
    service: Any                          # The actual service instance
    capabilities: list[str]
    registered_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    health: ServiceHealth = ServiceHealth.UNKNOWN
    health_checked_at: str = ""
    plugin_id: str = ""                   # Set if this service was registered by a plugin


class ServiceRegistry:
    """
    Kernel service registry.

    Services are registered by plugins (during plugin.initialize()) or
    directly by the Kernel during Phase 3 boot.

    Thread-safety: not needed (asyncio single-event-loop model).
    """

    def __init__(self) -> None:
        self._services: dict[str, ServiceRecord] = {}
        # capability → set of service_ids
        self._capability_index: dict[str, set[str]] = {}

    # ── Registration ───────────────────────────────────────────────────────────

    def register(
        self,
        service_id: str,
        service: Any,
        capabilities: list[str],
        plugin_id: str = "",
    ) -> None:
        """
        Register a service with its capabilities.

        Raises:
            ServiceConflictError: if service_id is already registered
            InvalidCapabilityError: if any capability string is malformed
        """
        if service_id in self._services:
            raise ServiceConflictError(service_id)

        for cap in capabilities:
            _validate_capability(cap)

        record = ServiceRecord(
            service_id=service_id,
            service=service,
            capabilities=list(capabilities),
            plugin_id=plugin_id,
        )
        self._services[service_id] = record

        for cap in capabilities:
            self._capability_index.setdefault(cap, set()).add(service_id)

        log.info(
            "Service registered",
            extra={"ctx_service_id": service_id, "ctx_capabilities": capabilities},
        )

    def deregister(self, service_id: str) -> None:
        """Remove a service and its capability index entries."""
        record = self._services.pop(service_id, None)
        if record is None:
            return  # idempotent
        for cap in record.capabilities:
            self._capability_index.get(cap, set()).discard(service_id)
        log.info("Service deregistered", extra={"ctx_service_id": service_id})

    def deregister_by_plugin(self, plugin_id: str) -> list[str]:
        """Remove all services registered by a given plugin. Returns removed IDs."""
        to_remove = [
            sid for sid, rec in self._services.items()
            if rec.plugin_id == plugin_id
        ]
        for sid in to_remove:
            self.deregister(sid)
        return to_remove

    # ── Discovery ──────────────────────────────────────────────────────────────

    def get(self, service_id: str) -> Any:
        """
        Return the service instance.

        Raises:
            ServiceNotFoundError: if service_id is not registered
        """
        record = self._services.get(service_id)
        if record is None:
            raise ServiceNotFoundError(service_id)
        return record.service

    def find_by_capability(self, capability: str) -> list[Any]:
        """
        Return all service instances that provide the given capability.

        Returns an empty list if no services match.
        """
        service_ids = self._capability_index.get(capability, set())
        return [self._services[sid].service for sid in service_ids if sid in self._services]

    def list_services(self) -> dict[str, list[str]]:
        """Return mapping of service_id → capabilities for all registered services."""
        return {sid: list(rec.capabilities) for sid, rec in self._services.items()}

    def get_record(self, service_id: str) -> ServiceRecord | None:
        return self._services.get(service_id)

    def all_records(self) -> list[ServiceRecord]:
        return list(self._services.values())

    # ── Health ─────────────────────────────────────────────────────────────────

    def update_health(self, service_id: str, health: ServiceHealth) -> None:
        record = self._services.get(service_id)
        if record:
            record.health = health
            record.health_checked_at = datetime.now(timezone.utc).isoformat()

    # ── Introspection ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        return {
            "service_count": len(self._services),
            "capability_count": len(self._capability_index),
            "services": {
                sid: {"capabilities": rec.capabilities, "health": rec.health.value}
                for sid, rec in self._services.items()
            },
        }
