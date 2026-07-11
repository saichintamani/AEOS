"""
AEOS Kernel — Lifecycle Management

Defines the lifecycle state machine for all kernel-managed components.
Every component in AEOS (kernel, plugins, services, agents) has a lifecycle
state that is tracked and enforced by the kernel.

State Machine:
    CREATED → INITIALIZING → READY → RUNNING → DRAINING → STOPPING → STOPPED
                                                                ↓
                                                             FAILED

Components may not skip states. Invalid transitions raise LifecycleError.
The LifecycleManager enforces valid transitions and records the full
transition history for auditing and debugging purposes.

Design Laws (from Architecture Constitution):
    - Law 6: Every operation is observable (transitions emit kernel events)
    - Law 7: Fail-safe defaults (invalid transitions are rejected, not silently accepted)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable


__all__ = [
    "LifecycleState",
    "LifecycleTransition",
    "LifecycleError",
    "ComponentLifecycle",
    "LifecycleManager",
    "LocalLifecycleManager",
    "VALID_TRANSITIONS",
]


# ---------------------------------------------------------------------------
# Lifecycle State Enum
# ---------------------------------------------------------------------------


class LifecycleState(Enum):
    """
    Enumeration of all possible lifecycle states for a kernel-managed component.

    Every component (kernel, plugin, service, agent) transitions through these
    states in order. The state machine is enforced by LifecycleManager — no
    component may skip a state or make an invalid transition.

    Transition graph:
        CREATED → INITIALIZING → READY → RUNNING → DRAINING → STOPPING → STOPPED
                       ↓                    ↓           ↓          ↓
                    FAILED               FAILED      FAILED     FAILED
    """

    CREATED = "created"
    """
    The component has been instantiated but startup has not begun.
    This is the default state for all components at construction time.
    The component may not serve requests in this state.
    """

    INITIALIZING = "initializing"
    """
    The component is executing its initialization logic (e.g., loading config,
    opening connections, registering with the kernel). The component may not
    serve requests in this state.
    """

    READY = "ready"
    """
    The component has completed initialization and passed health checks.
    It is prepared to receive work but the kernel has not yet started routing
    traffic to it. This state is transient — the kernel immediately transitions
    to RUNNING after verifying health in Phase 4.
    """

    RUNNING = "running"
    """
    The component is fully operational and actively serving requests.
    This is the normal steady-state during platform operation.
    """

    DRAINING = "draining"
    """
    The component is no longer accepting new work but is completing in-flight
    requests. This state is entered at the beginning of graceful shutdown.
    Duration is bounded by drain_timeout_seconds.
    """

    STOPPING = "stopping"
    """
    The component is executing its shutdown logic (closing connections,
    flushing buffers, deregistering from the kernel). In-flight work has
    either completed or been abandoned.
    """

    STOPPED = "stopped"
    """
    The component has fully shut down. It holds no resources and serves
    no requests. This is a terminal state — a stopped component cannot
    be restarted without reinstantiation.
    """

    FAILED = "failed"
    """
    The component encountered an unrecoverable error. It may have partially
    initialized or may have been running when the failure occurred.
    The kernel will attempt to recover (restart) optional components or
    will initiate platform shutdown for required components.
    This is a terminal state from the current instance's perspective.
    """


# ---------------------------------------------------------------------------
# Valid State Transitions
# ---------------------------------------------------------------------------


VALID_TRANSITIONS: dict[LifecycleState, set[LifecycleState]] = {
    LifecycleState.CREATED: {
        LifecycleState.INITIALIZING,
        LifecycleState.FAILED,
    },
    LifecycleState.INITIALIZING: {
        LifecycleState.READY,
        LifecycleState.FAILED,
    },
    LifecycleState.READY: {
        LifecycleState.RUNNING,
        LifecycleState.STOPPING,  # Shutdown before ever running
        LifecycleState.FAILED,
    },
    LifecycleState.RUNNING: {
        LifecycleState.DRAINING,
        LifecycleState.STOPPING,  # Forced stop, skipping drain
        LifecycleState.FAILED,
    },
    LifecycleState.DRAINING: {
        LifecycleState.STOPPING,
        LifecycleState.FAILED,
    },
    LifecycleState.STOPPING: {
        LifecycleState.STOPPED,
        LifecycleState.FAILED,
    },
    LifecycleState.STOPPED: set(),   # Terminal state
    LifecycleState.FAILED: set(),    # Terminal state
}
"""
Mapping of each LifecycleState to the set of states it may transition to.
Any transition not present in this map is invalid and will raise LifecycleError.
"""


# ---------------------------------------------------------------------------
# Lifecycle Transition Dataclass
# ---------------------------------------------------------------------------


@dataclass
class LifecycleTransition:
    """
    A record of a single lifecycle state transition for a component.

    The LifecycleManager appends one LifecycleTransition to a component's
    history for every successful state change. This history is immutable
    and used for auditing, debugging, and uptime calculation.
    """

    component_id: str
    """The unique identifier of the component that transitioned."""

    from_state: LifecycleState
    """The state the component was in before the transition."""

    to_state: LifecycleState
    """The state the component entered as a result of this transition."""

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """ISO 8601 UTC timestamp of when the transition occurred."""

    reason: str = ""
    """
    Human-readable reason for the transition. Required for FAILED transitions.
    Optional but recommended for all transitions to aid debugging.

    Examples:
        "Plugin initialization complete"
        "Health check passed (Phase 4)"
        "Graceful shutdown initiated by operator"
        "Database connection refused after 3 retries"
    """

    triggered_by: str = "kernel"
    """
    The actor that triggered this transition.
    Typically "kernel", "operator", or a component ID.
    """


# ---------------------------------------------------------------------------
# Lifecycle Exceptions
# ---------------------------------------------------------------------------


class LifecycleError(Exception):
    """
    Raised when a lifecycle operation violates the state machine contract.

    Common causes:
        - Attempting an invalid state transition (e.g., CREATED → RUNNING).
        - Attempting any transition from a terminal state (STOPPED or FAILED).
        - Calling a lifecycle method on a component in an incompatible state.

    Attributes:
        component_id: The component whose lifecycle was violated.
        current_state: The component's current state at the time of the error.
        attempted_transition: The state the caller tried to transition to.
    """

    def __init__(
        self,
        message: str,
        component_id: str = "",
        current_state: LifecycleState | None = None,
        attempted_transition: LifecycleState | None = None,
    ) -> None:
        super().__init__(message)
        self.component_id = component_id
        self.current_state = current_state
        self.attempted_transition = attempted_transition


# ---------------------------------------------------------------------------
# ComponentLifecycle Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ComponentLifecycle(Protocol):
    """
    Runtime duck-typing protocol for any kernel-managed component.

    All components managed by the Kernel (plugins, services, agents) must
    implement this protocol. It is used by the Kernel to inspect and
    trigger lifecycle transitions without knowing the component's concrete type.

    This is a structural protocol — components do not need to explicitly
    inherit from it. The `runtime_checkable` decorator enables
    `isinstance(obj, ComponentLifecycle)` checks.
    """

    @property
    def lifecycle_state(self) -> LifecycleState:
        """
        The current lifecycle state of this component.

        Must be readable at any time, including before initialization and
        after failure. Must never raise.
        """
        ...

    async def on_lifecycle_transition(
        self,
        transition: LifecycleTransition,
    ) -> None:
        """
        Called by the LifecycleManager immediately after a successful transition.

        Components implement this method to perform state-specific setup or
        teardown. For example, a plugin might open database connections when
        transitioning to INITIALIZING, or flush write buffers when entering DRAINING.

        This method must not raise — exceptions are caught, logged, and suppressed
        by the LifecycleManager. It must not trigger further lifecycle transitions
        (to avoid recursion). It must complete within on_transition_timeout_seconds.

        Args:
            transition: The LifecycleTransition that just occurred, including
                from_state, to_state, reason, and timestamp.
        """
        ...


# ---------------------------------------------------------------------------
# LifecycleManager Abstract Base Class
# ---------------------------------------------------------------------------


class LifecycleManager(ABC):
    """
    Abstract base for the AEOS Kernel Lifecycle Manager.

    The LifecycleManager is the authoritative controller for component state
    machines. It validates transitions against VALID_TRANSITIONS, records
    transition history, calls component on_lifecycle_transition() hooks, and
    emits kernel events for every transition.

    One LifecycleManager instance is created by the Kernel during Phase 1 boot
    and manages the lifecycle of the Kernel itself and all registered components.

    Concrete implementations:
        - LocalLifecycleManager: In-process state machine (current phase).
        - DistributedLifecycleManager: State persisted to distributed store (future).
    """

    @abstractmethod
    async def transition(
        self,
        component_id: str,
        to_state: LifecycleState,
        reason: str = "",
        triggered_by: str = "kernel",
    ) -> LifecycleTransition:
        """
        Attempt to transition a registered component to a new lifecycle state.

        Validates the transition against the VALID_TRANSITIONS map, records it
        in the component's transition history, calls the component's
        on_lifecycle_transition() hook, and emits a kernel.lifecycle.transition event.

        Args:
            component_id: The unique identifier of the component to transition.
            to_state: The target lifecycle state.
            reason: Human-readable explanation for the transition.
            triggered_by: The actor initiating the transition (e.g., "kernel",
                "operator", or another component_id).

        Returns:
            The LifecycleTransition record that was created.

        Raises:
            LifecycleError: The transition is not valid from the component's
                current state, or the component is not registered.
            ComponentNotFoundError: component_id is not registered with this manager.
        """
        ...

    @abstractmethod
    def can_transition(
        self,
        component_id: str,
        to_state: LifecycleState,
    ) -> bool:
        """
        Check whether a transition is valid without executing it.

        This method is synchronous and non-blocking. It consults the
        VALID_TRANSITIONS map and the component's current state.

        Args:
            component_id: The unique identifier of the component to check.
            to_state: The target lifecycle state to check.

        Returns:
            True if the transition is valid, False otherwise.

        Note:
            A return value of True does not guarantee that calling transition()
            will succeed — concurrent transitions may change the component's
            state between this check and the actual transition call.
        """
        ...

    @abstractmethod
    def current_state(self, component_id: str) -> LifecycleState:
        """
        Return the current lifecycle state of a registered component.

        Args:
            component_id: The unique identifier of the component.

        Returns:
            The current LifecycleState.

        Raises:
            ComponentNotFoundError: component_id is not registered.
        """
        ...

    @abstractmethod
    def history(
        self,
        component_id: str,
        limit: int = 100,
    ) -> list[LifecycleTransition]:
        """
        Return the transition history for a component in chronological order.

        Args:
            component_id: The unique identifier of the component.
            limit: Maximum number of transitions to return (most recent first
                if limit is less than total history). Default: 100.

        Returns:
            List of LifecycleTransition records, most recent last.

        Raises:
            ComponentNotFoundError: component_id is not registered.
        """
        ...

    @abstractmethod
    def register_component(
        self,
        component_id: str,
        component: ComponentLifecycle,
        initial_state: LifecycleState = LifecycleState.CREATED,
    ) -> None:
        """
        Register a component with the lifecycle manager.

        Must be called before any transitions can be applied to the component.
        The component is registered in the given initial_state (default: CREATED).

        Args:
            component_id: Unique identifier for this component. Must be unique
                across all registered components.
            component: The component instance implementing ComponentLifecycle.
            initial_state: The state to register the component in. Almost always
                CREATED — only override in specific recovery scenarios.

        Raises:
            ValueError: component_id is already registered.
        """
        ...

    @abstractmethod
    def deregister_component(self, component_id: str) -> None:
        """
        Remove a component from the lifecycle manager.

        Should only be called after the component has reached STOPPED or FAILED.
        Deregistering a component in any other state emits a warning.

        Args:
            component_id: The unique identifier of the component to remove.

        Raises:
            ComponentNotFoundError: component_id is not registered.
        """
        ...

    @abstractmethod
    def on_transition(
        self,
        callback: Callable[[LifecycleTransition], Coroutine],
    ) -> None:
        """
        Register a global callback invoked after every lifecycle transition.

        The callback is invoked after the component's own on_lifecycle_transition()
        hook. Multiple callbacks may be registered. Callbacks are invoked
        concurrently. Callback failures are caught, logged, and suppressed.

        Args:
            callback: An async callable that receives a LifecycleTransition.
        """
        ...

    @abstractmethod
    def list_components(self) -> dict[str, LifecycleState]:
        """
        Return a mapping of all registered component IDs to their current state.

        Returns:
            Dictionary of component_id → LifecycleState for all registered
            components, in registration order.
        """
        ...


# ---------------------------------------------------------------------------
# LocalLifecycleManager — In-process concrete implementation
# ---------------------------------------------------------------------------


class LocalLifecycleManager(LifecycleManager):
    """
    In-process lifecycle state machine. Single-node, single-process.

    Stores all component states in memory. Validates every transition
    against VALID_TRANSITIONS. Records full transition history.
    Invokes registered global callbacks after each transition.

    This is the concrete implementation used in Phase 8.x.
    DistributedLifecycleManager (future phase) will persist state to Redis.
    """

    def __init__(self) -> None:
        self._states: dict[str, LifecycleState] = {}
        self._histories: dict[str, list[LifecycleTransition]] = {}
        self._callbacks: list[Callable[[LifecycleTransition], Any]] = []

    async def transition(
        self,
        component_id: str,
        to_state: LifecycleState,
        reason: str = "",
        triggered_by: str = "kernel",
    ) -> LifecycleTransition:
        current = self._states.get(component_id, LifecycleState.CREATED)
        valid = VALID_TRANSITIONS.get(current, set())
        if to_state not in valid:
            raise LifecycleError(
                f"Invalid transition for '{component_id}': {current.value} → {to_state.value}",
                component_id=component_id,
                current_state=current,
                attempted_transition=to_state,
            )

        t = LifecycleTransition(
            component_id=component_id,
            from_state=current,
            to_state=to_state,
            reason=reason,
            triggered_by=triggered_by,
        )
        self._states[component_id] = to_state
        self._histories.setdefault(component_id, []).append(t)

        for cb in self._callbacks:
            try:
                result = cb(t)
                if hasattr(result, "__await__"):
                    await result  # type: ignore[misc]
            except Exception:
                pass  # callback failures must not abort transitions

        return t

    def can_transition(self, component_id: str, to_state: LifecycleState) -> bool:
        current = self._states.get(component_id, LifecycleState.CREATED)
        return to_state in VALID_TRANSITIONS.get(current, set())

    def current_state(self, component_id: str) -> LifecycleState:
        if component_id not in self._states:
            raise LifecycleError(
                f"Component '{component_id}' is not registered",
                component_id=component_id,
            )
        return self._states[component_id]

    def history(self, component_id: str, limit: int = 100) -> list[LifecycleTransition]:
        h = self._histories.get(component_id, [])
        return h[-limit:]

    def register_component(
        self,
        component_id: str,
        component: Any,
        initial_state: LifecycleState = LifecycleState.CREATED,
    ) -> None:
        if component_id in self._states:
            raise ValueError(f"Component '{component_id}' is already registered")
        self._states[component_id] = initial_state
        self._histories[component_id] = []

    def deregister_component(self, component_id: str) -> None:
        self._states.pop(component_id, None)
        self._histories.pop(component_id, None)

    def on_transition(
        self,
        callback: Callable[[LifecycleTransition], Coroutine],
    ) -> None:
        self._callbacks.append(callback)

    def list_components(self) -> dict[str, LifecycleState]:
        return dict(self._states)
