"""
AEOS Kernel — Resource Manager

Tracks and enforces resource budgets across all platform components.
Prevents any single agent, plugin, or service from monopolizing shared
compute capacity (memory, CPU, LLM API tokens, concurrent tasks).

Design:
  - ResourceRequest carries what is needed and by whom (priority 1–10)
  - ResourceGrant is issued when capacity and policy allow
  - Grants have a TTL; uncleaned grants are auto-reclaimed at expiry
  - All allocations are tracked in an in-memory ledger
  - No I/O; this is purely computational accounting
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.core.logger import get_logger
from app.kernel.exceptions import ResourceGrantNotFoundError

if TYPE_CHECKING:
    pass

__all__ = [
    "ResourceRequest",
    "ResourceGrant",
    "ResourceCapacity",
    "ResourceManager",
]

log = get_logger(__name__)

# Default TTL for a grant if not specified: 10 minutes
_DEFAULT_GRANT_TTL_SECONDS = 600

# High-watermark at which low-priority requests are denied (90 %)
_HIGH_WATERMARK = 0.90


@dataclass
class ResourceRequest:
    """What a component is asking for."""
    requester_id: str           # agent_id, plugin_id, or service_id
    memory_bytes: int = 0
    cpu_millicores: int = 0
    gpu_count: int = 0
    llm_tokens: int = 0         # tokens per minute
    concurrent_tasks: int = 1
    priority: int = 5           # 1 (low) – 10 (kernel reserved)
    ttl_seconds: float = _DEFAULT_GRANT_TTL_SECONDS
    trace_id: str = ""


@dataclass
class ResourceGrant:
    """Outcome of a resource request evaluation."""
    grant_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requester_id: str = ""
    granted: bool = False
    denied_reason: str = ""
    memory_bytes: int = 0
    cpu_millicores: int = 0
    gpu_count: int = 0
    llm_tokens: int = 0
    concurrent_tasks: int = 0
    expires_at: float = 0.0     # UNIX timestamp
    issued_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


@dataclass
class ResourceCapacity:
    """Platform-wide total capacity."""
    memory_bytes: int = 4 * 1024 ** 3          # 4 GB default
    cpu_millicores: int = 4_000                 # 4 cores default
    gpu_count: int = 0
    llm_tokens_per_minute: int = 100_000
    max_concurrent_tasks: int = 50


class _Ledger:
    """Tracks active grants."""

    def __init__(self) -> None:
        self._grants: dict[str, ResourceGrant] = {}

    def add(self, grant: ResourceGrant) -> None:
        self._grants[grant.grant_id] = grant

    def remove(self, grant_id: str) -> ResourceGrant | None:
        return self._grants.pop(grant_id, None)

    def get(self, grant_id: str) -> ResourceGrant | None:
        return self._grants.get(grant_id)

    def all(self) -> list[ResourceGrant]:
        return list(self._grants.values())

    def reclaim_expired(self) -> list[ResourceGrant]:
        """Remove and return all expired grants."""
        expired = [g for g in self._grants.values() if g.is_expired]
        for g in expired:
            del self._grants[g.grant_id]
        return expired

    def by_requester(self, requester_id: str) -> list[ResourceGrant]:
        return [g for g in self._grants.values() if g.requester_id == requester_id]

    @property
    def allocated_memory(self) -> int:
        return sum(g.memory_bytes for g in self._grants.values())

    @property
    def allocated_cpu(self) -> int:
        return sum(g.cpu_millicores for g in self._grants.values())

    @property
    def allocated_tasks(self) -> int:
        return sum(g.concurrent_tasks for g in self._grants.values())


class ResourceManager:
    """
    Kernel resource accounting layer.

    Enforces per-requester quotas and platform-wide capacity limits.
    Issues ResourceGrant objects; callers must call release() when done.
    """

    _DEFAULT_QUOTAS: dict[str, dict] = {
        "default": {
            "memory_bytes": 2 * 1024 ** 3,    # 2 GB
            "cpu_millicores": 2_000,
            "concurrent_tasks": 5,
            "llm_tokens": 50_000,
        }
    }

    def __init__(self, capacity: ResourceCapacity | None = None) -> None:
        self._capacity = capacity or ResourceCapacity()
        self._ledger = _Ledger()
        self._quotas: dict[str, dict] = dict(self._DEFAULT_QUOTAS)

    # ── Request ────────────────────────────────────────────────────────────────

    async def request(self, req: ResourceRequest) -> ResourceGrant:
        """
        Evaluate a resource request against capacity and quotas.

        Always returns a ResourceGrant. Check grant.granted to determine
        whether the request was approved.
        """
        # Reclaim expired grants first
        expired = self._ledger.reclaim_expired()
        if expired:
            log.debug("Reclaimed expired grants", extra={"ctx_count": len(expired)})

        # Check capacity high-watermark for low-priority requests
        usage_ratio = self._usage_ratio()
        if usage_ratio >= _HIGH_WATERMARK and req.priority < 6:
            return self._deny(req, f"capacity_high_watermark ({usage_ratio:.0%} used)")

        # Per-requester quota check
        quota = self._quotas.get(req.requester_id, self._quotas["default"])
        active_grants = self._ledger.by_requester(req.requester_id)
        already_allocated_memory = sum(g.memory_bytes for g in active_grants)
        if req.memory_bytes and already_allocated_memory + req.memory_bytes > quota["memory_bytes"]:
            return self._deny(req, f"memory_quota_exceeded for {req.requester_id}")

        # Platform capacity check
        if req.memory_bytes and (self._ledger.allocated_memory + req.memory_bytes > self._capacity.memory_bytes):
            return self._deny(req, "platform_memory_capacity_exceeded")

        if req.cpu_millicores and (self._ledger.allocated_cpu + req.cpu_millicores > self._capacity.cpu_millicores):
            return self._deny(req, "platform_cpu_capacity_exceeded")

        if req.concurrent_tasks and (self._ledger.allocated_tasks + req.concurrent_tasks > self._capacity.max_concurrent_tasks):
            return self._deny(req, "platform_concurrent_task_limit_exceeded")

        # Grant
        grant = ResourceGrant(
            requester_id=req.requester_id,
            granted=True,
            memory_bytes=req.memory_bytes,
            cpu_millicores=req.cpu_millicores,
            gpu_count=req.gpu_count,
            llm_tokens=req.llm_tokens,
            concurrent_tasks=req.concurrent_tasks,
            expires_at=time.time() + req.ttl_seconds,
        )
        self._ledger.add(grant)

        log.debug(
            "Resource grant issued",
            extra={
                "ctx_grant_id": grant.grant_id,
                "ctx_requester": req.requester_id,
                "ctx_memory_mb": req.memory_bytes // (1024 ** 2),
            },
        )
        return grant

    # ── Release ────────────────────────────────────────────────────────────────

    async def release(self, grant_id: str) -> None:
        """
        Return granted resources to the pool.

        Raises:
            ResourceGrantNotFoundError: if grant_id is completely unknown
        """
        grant = self._ledger.remove(grant_id)
        if grant is None:
            raise ResourceGrantNotFoundError(grant_id)
        log.debug("Resource grant released", extra={"ctx_grant_id": grant_id})

    def release_by_requester(self, requester_id: str) -> int:
        """Revoke all grants held by a specific requester. Returns count."""
        grants = self._ledger.by_requester(requester_id)
        for g in grants:
            self._ledger.remove(g.grant_id)
        return len(grants)

    # ── Quota configuration ────────────────────────────────────────────────────

    def set_quota(self, requester_id: str, quota: dict) -> None:
        self._quotas[requester_id] = quota

    # ── Introspection ──────────────────────────────────────────────────────────

    def _usage_ratio(self) -> float:
        if self._capacity.memory_bytes == 0:
            return 0.0
        return self._ledger.allocated_memory / self._capacity.memory_bytes

    def summarize(self) -> dict:
        self._ledger.reclaim_expired()
        return {
            "capacity": {
                "memory_bytes": self._capacity.memory_bytes,
                "cpu_millicores": self._capacity.cpu_millicores,
                "max_concurrent_tasks": self._capacity.max_concurrent_tasks,
            },
            "allocated": {
                "memory_bytes": self._ledger.allocated_memory,
                "cpu_millicores": self._ledger.allocated_cpu,
                "concurrent_tasks": self._ledger.allocated_tasks,
            },
            "usage_ratio": round(self._usage_ratio(), 4),
            "active_grants": len(self._ledger.all()),
        }

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _deny(req: ResourceRequest, reason: str) -> ResourceGrant:
        log.warning(
            "Resource request denied",
            extra={"ctx_requester": req.requester_id, "ctx_reason": reason},
        )
        return ResourceGrant(
            requester_id=req.requester_id,
            granted=False,
            denied_reason=reason,
            expires_at=0.0,
        )
