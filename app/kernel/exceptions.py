"""
AEOS Kernel — Exception Hierarchy

All exceptions raised by the AEOS Kernel. These are typed, structured
exceptions that carry enough context for callers to make recovery decisions.

Design principle: Every exception type is distinct. Callers never catch
bare `Exception` from the kernel — they catch the specific type they can
handle, and let others propagate.
"""

from __future__ import annotations


__all__ = [
    # Boot / lifecycle
    "KernelError",
    "KernelBootError",
    "KernelStateError",
    "KernelShutdownError",
    # Plugin
    "PluginError",
    "PluginConflictError",
    "PluginDependencyError",
    "PluginInitializationError",
    "PluginNotFoundError",
    "PluginShutdownError",
    "PluginVersionError",
    # Service
    "ServiceError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "ServiceDegradedError",
    "InvalidCapabilityError",
    # Resource
    "ResourceError",
    "ResourceDeniedError",
    "ResourceGrantNotFoundError",
    "ResourceExhaustedError",
    # Policy
    "PolicyError",
    "PolicyViolationError",
    "PolicyEvaluationError",
    # Scheduler
    "SchedulerError",
    "TaskSubmissionError",
    "ConcurrencyLimitError",
    # Event
    "EventBusError",
    "EventBusSaturatedError",
]


# ── Base ───────────────────────────────────────────────────────────────────────

class KernelError(Exception):
    """Root exception for all AEOS Kernel errors."""

    def __init__(self, message: str, code: str = "", context: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.context: dict = context or {}

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r}, message={str(self)!r})"


# ── Boot / Lifecycle ───────────────────────────────────────────────────────────

class KernelBootError(KernelError):
    """Raised when the kernel fails to complete its boot sequence."""

    def __init__(self, phase: int, reason: str, details: str = "") -> None:
        msg = f"Kernel boot failed in phase {phase}: {reason}"
        if details:
            msg += f" — {details}"
        super().__init__(msg, code=f"BOOT_PHASE_{phase}_FAILED", context={"phase": phase, "reason": reason})
        self.phase = phase
        self.reason = reason


class KernelStateError(KernelError):
    """Raised when a kernel operation is invalid given the current lifecycle state."""

    def __init__(self, current_state: str, required_state: str, operation: str) -> None:
        super().__init__(
            f"Operation '{operation}' requires kernel state {required_state!r}, "
            f"but current state is {current_state!r}.",
            code="INVALID_KERNEL_STATE",
            context={"current_state": current_state, "required_state": required_state, "operation": operation},
        )


class KernelShutdownError(KernelError):
    """Raised when the kernel shutdown sequence encounters an unrecoverable error."""


# ── Plugin ─────────────────────────────────────────────────────────────────────

class PluginError(KernelError):
    """Base for all plugin-related errors."""


class PluginConflictError(PluginError):
    """Raised when a plugin with the same ID is already registered."""

    def __init__(self, plugin_id: str) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' is already registered. Plugin IDs must be unique.",
            code="PLUGIN_CONFLICT",
            context={"plugin_id": plugin_id},
        )
        self.plugin_id = plugin_id


class PluginDependencyError(PluginError):
    """Raised when one or more plugin dependencies cannot be satisfied."""

    def __init__(self, plugin_id: str, missing: list[str]) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' requires {missing} but these are not loaded.",
            code="PLUGIN_DEPENDENCY_MISSING",
            context={"plugin_id": plugin_id, "missing_dependencies": missing},
        )
        self.plugin_id = plugin_id
        self.missing = missing


class PluginInitializationError(PluginError):
    """Raised when a plugin fails to initialize."""

    def __init__(self, plugin_id: str, reason: str) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' failed to initialize: {reason}",
            code="PLUGIN_INIT_FAILED",
            context={"plugin_id": plugin_id, "reason": reason},
        )
        self.plugin_id = plugin_id


class PluginNotFoundError(PluginError):
    """Raised when a requested plugin is not in the registry."""

    def __init__(self, plugin_id: str) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' is not registered.",
            code="PLUGIN_NOT_FOUND",
            context={"plugin_id": plugin_id},
        )
        self.plugin_id = plugin_id


class PluginShutdownError(PluginError):
    """Raised when a plugin fails to shut down cleanly."""

    def __init__(self, plugin_id: str, reason: str) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' shutdown error: {reason}",
            code="PLUGIN_SHUTDOWN_FAILED",
            context={"plugin_id": plugin_id, "reason": reason},
        )
        self.plugin_id = plugin_id


class PluginVersionError(PluginError):
    """Raised when a plugin requires a newer AEOS version than is running."""

    def __init__(self, plugin_id: str, required: str, current: str) -> None:
        super().__init__(
            f"Plugin '{plugin_id}' requires AEOS >= {required}, but running {current}.",
            code="PLUGIN_VERSION_INCOMPATIBLE",
            context={"plugin_id": plugin_id, "required_version": required, "current_version": current},
        )


# ── Service ────────────────────────────────────────────────────────────────────

class ServiceError(KernelError):
    """Base for all service-registry errors."""


class ServiceConflictError(ServiceError):
    """Raised when a service with the same ID is already registered."""

    def __init__(self, service_id: str) -> None:
        super().__init__(
            f"Service '{service_id}' is already registered.",
            code="SERVICE_CONFLICT",
            context={"service_id": service_id},
        )
        self.service_id = service_id


class ServiceNotFoundError(ServiceError):
    """Raised when a requested service is not in the registry."""

    def __init__(self, service_id: str) -> None:
        super().__init__(
            f"Service '{service_id}' is not registered.",
            code="SERVICE_NOT_FOUND",
            context={"service_id": service_id},
        )
        self.service_id = service_id


class ServiceDegradedError(ServiceError):
    """Raised when a service is registered but reporting degraded health."""

    def __init__(self, service_id: str, reason: str = "") -> None:
        msg = f"Service '{service_id}' is degraded."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg, code="SERVICE_DEGRADED", context={"service_id": service_id})
        self.service_id = service_id


class InvalidCapabilityError(ServiceError):
    """Raised when a capability string fails schema validation."""

    def __init__(self, capability: str, reason: str) -> None:
        super().__init__(
            f"Capability string {capability!r} is invalid: {reason}",
            code="INVALID_CAPABILITY",
            context={"capability": capability, "reason": reason},
        )


# ── Resource ───────────────────────────────────────────────────────────────────

class ResourceError(KernelError):
    """Base for all resource-management errors."""


class ResourceDeniedError(ResourceError):
    """Raised when a resource request is denied (returned as value, not raised — for API callers)."""

    def __init__(self, requester_id: str, reason: str) -> None:
        super().__init__(
            f"Resource request from '{requester_id}' denied: {reason}",
            code="RESOURCE_DENIED",
            context={"requester_id": requester_id, "reason": reason},
        )


class ResourceGrantNotFoundError(ResourceError):
    """Raised when release_resources is called with an unknown grant_id."""

    def __init__(self, grant_id: str) -> None:
        super().__init__(
            f"Resource grant '{grant_id}' not found.",
            code="RESOURCE_GRANT_NOT_FOUND",
            context={"grant_id": grant_id},
        )
        self.grant_id = grant_id


class ResourceExhaustedError(ResourceError):
    """Raised when platform-level resource capacity is entirely exhausted."""


# ── Policy ─────────────────────────────────────────────────────────────────────

class PolicyError(KernelError):
    """Base for all policy-engine errors."""


class PolicyViolationError(PolicyError):
    """Raised when an operation is hard-blocked by a policy (not returned as value)."""

    def __init__(self, action: str, policy_id: str, reason: str) -> None:
        super().__init__(
            f"Action '{action}' blocked by policy '{policy_id}': {reason}",
            code="POLICY_VIOLATION",
            context={"action": action, "policy_id": policy_id, "reason": reason},
        )
        self.policy_id = policy_id


class PolicyEvaluationError(PolicyError):
    """Raised when the policy engine itself encounters an internal error."""


# ── Scheduler ──────────────────────────────────────────────────────────────────

class SchedulerError(KernelError):
    """Base for all scheduler errors."""


class TaskSubmissionError(SchedulerError):
    """Raised when a task cannot be submitted to the scheduler."""


class ConcurrencyLimitError(SchedulerError):
    """Raised when the scheduler's concurrency limit has been reached."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Concurrency limit of {limit} active tasks has been reached.",
            code="CONCURRENCY_LIMIT_EXCEEDED",
            context={"limit": limit},
        )


# ── Event Bus ──────────────────────────────────────────────────────────────────

class EventBusError(KernelError):
    """Base for all event-bus errors."""


class EventBusSaturatedError(EventBusError):
    """Raised when the event bus queue is at capacity and events are being dropped."""

    def __init__(self, queue_depth: int, max_depth: int) -> None:
        super().__init__(
            f"Event bus saturated: {queue_depth}/{max_depth} events queued.",
            code="EVENT_BUS_SATURATED",
            context={"queue_depth": queue_depth, "max_depth": max_depth},
        )
