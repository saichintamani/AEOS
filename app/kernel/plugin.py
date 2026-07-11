"""
AEOS Kernel — Plugin System

Defines the plugin contract. Every extension to AEOS is a plugin.
Plugins are the primary extension mechanism for the platform.

Built-in plugins (loaded automatically by the kernel):
    - rag_plugin       — Knowledge retrieval services
    - ml_plugin        — ML training and inference services
    - github_plugin    — GitHub analysis services
    - osip_plugin      — Software intelligence services

External plugins (installed by users/operators):
    - Loaded from plugins/ directory at startup
    - Must conform to BasePlugin contract
    - Declared via PluginManifest

Design Law: No hardcoded modules. Everything is a plugin (Law 3 variant).

The plugin lifecycle is managed entirely by the Kernel:
    UNLOADED → LOADING → READY → (DEGRADED?) → UNLOADING → UNLOADED
                                      ↓
                                   FAILED

See docs/architecture/005-KERNEL.md §7 for the full plugin contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Avoid circular import: kernel.py imports plugin.py, plugin.py imports
    # kernel.py only for type hints. Use TYPE_CHECKING guard.
    from app.kernel.kernel import AEOSKernel, KernelEvent

from app.kernel.lifecycle import LifecycleState


__all__ = [
    "PluginManifest",
    "PluginStatus",
    "BasePlugin",
    "PluginRegistry",
    "PluginConflictError",
    "PluginDependencyError",
    "PluginInitializationError",
    "PluginNotFoundError",
    "PluginShutdownError",
]


# ---------------------------------------------------------------------------
# Plugin Manifest
# ---------------------------------------------------------------------------


@dataclass
class PluginManifest:
    """
    Declarative identity and metadata for an AEOS plugin.

    The manifest is the contract between a plugin and the AEOS Kernel.
    It is parsed from `plugin.yaml` in the plugin's directory before
    any plugin code is imported. The Kernel validates the manifest
    schema before proceeding with plugin loading.

    Manifest file location (convention):
        plugins/{plugin_id}/plugin.yaml

    Required fields: id, name, version, min_aeos_version
    All other fields have defaults and are optional.
    """

    id: str
    """
    Globally unique plugin identifier.
    Convention: lowercase, underscore-separated (e.g., "rag_plugin").
    Must be unique across all loaded plugins. Conflict raises PluginConflictError.
    """

    name: str
    """
    Human-readable plugin name (e.g., "RAG Knowledge Plugin").
    Used in logs, admin UIs, and error messages.
    """

    version: str
    """
    SemVer version string (e.g., "1.2.3").
    Must be incremented for every published change.
    The Kernel uses this for compatibility checks and audit logs.
    """

    min_aeos_version: str
    """
    Minimum AEOS platform version this plugin requires (SemVer).
    The Kernel rejects plugins where min_aeos_version > current AEOS version.
    Example: "2.0.0" means this plugin requires AEOS 2.x or later.
    """

    description: str = ""
    """
    One-sentence human-readable description of what this plugin provides.
    Displayed in plugin registry listings and admin dashboards.
    """

    author: str = ""
    """
    Plugin author or team identifier (e.g., "Platform Team <platform@example.com>").
    """

    dependencies: list[str] = field(default_factory=list)
    """
    List of plugin IDs that must be loaded before this plugin.
    The Kernel resolves these into a topological load order.
    A dependency cycle raises PluginDependencyError at boot.

    Example: ["rag_plugin", "ml_plugin"]
    """

    capabilities: list[str] = field(default_factory=list)
    """
    List of dot-namespaced capability strings this plugin provides.
    Used to populate the CapabilityRegistry during service registration.
    Convention: "domain.action" (e.g., "rag.query", "ml.predict").

    Components can discover this plugin's services by calling:
        kernel.find_by_capability("rag.query")
    """

    config_schema: dict[str, Any] = field(default_factory=dict)
    """
    JSON Schema definition for this plugin's configuration block.
    The Kernel validates the plugin's config section in aeos.yaml against
    this schema before calling plugin.initialize(). An invalid config
    raises PluginInitializationError before any plugin code runs.

    Example:
        {
            "type": "object",
            "required": ["api_key"],
            "properties": {
                "api_key": {"type": "string"},
                "timeout_seconds": {"type": "integer", "default": 30},
            }
        }
    """

    entry_point: str = ""
    """
    Python import path for the plugin's BasePlugin implementation.
    Convention: "plugins.{plugin_id}.plugin:{PluginClassName}"
    If empty, the Kernel looks for a class named after the plugin_id
    in `plugins/{plugin_id}/plugin.py`.
    """

    tags: list[str] = field(default_factory=list)
    """
    Optional categorization tags for filtering in admin UIs.
    Example: ["ai", "retrieval", "vector-search"]
    """


# ---------------------------------------------------------------------------
# Plugin Status Enum
# ---------------------------------------------------------------------------


class PluginStatus(Enum):
    """
    Operational health status of a loaded plugin.

    Returned by BasePlugin.health(). The Kernel uses this to determine
    whether a plugin can serve requests and whether intervention is needed.

    Note: This is distinct from LifecycleState. A plugin can be in
    LifecycleState.RUNNING while reporting PluginStatus.DEGRADED (it is
    running but experiencing partial failures, e.g., a flaky external API).
    """

    UNLOADED = "unloaded"
    """
    Plugin is not loaded. Used as the initial status before loading
    and after successful unloading.
    """

    LOADING = "loading"
    """
    Plugin is currently executing initialize(). Not yet ready for requests.
    """

    READY = "ready"
    """
    Plugin is healthy and fully operational. All capabilities are available.
    This is the expected steady-state status for a loaded plugin.
    """

    DEGRADED = "degraded"
    """
    Plugin is operational but experiencing partial failures.
    Some capabilities may be unavailable or operating with reduced performance.
    The Kernel logs a warning and may alert, but does not unload the plugin.
    Example: A caching layer is down, so responses are slower than normal.
    """

    FAILED = "failed"
    """
    Plugin has experienced an unrecoverable failure.
    No capabilities are available. The Kernel will attempt restart
    (if configured) or mark the plugin permanently failed.
    """

    UNLOADING = "unloading"
    """
    Plugin is currently executing shutdown(). Not accepting new requests.
    """


# ---------------------------------------------------------------------------
# BasePlugin Abstract Base Class
# ---------------------------------------------------------------------------


class BasePlugin(ABC):
    """
    Abstract base class for all AEOS plugins.

    Every plugin — built-in or external — must subclass BasePlugin and
    implement all abstract methods. The Kernel interacts with plugins
    exclusively through this interface.

    Lifecycle:
        1. Plugin class is instantiated (no kernel access yet).
        2. Kernel calls initialize(kernel) — plugin sets up and registers services.
        3. Kernel calls health() — must return PluginStatus.READY.
        4. Plugin receives kernel events via on_kernel_event().
        5. Kernel calls shutdown() — plugin releases resources.

    Thread safety:
        All methods may be called from the Kernel's async event loop.
        initialize() and shutdown() are called with await.
        health() and manifest are called synchronously.
        on_kernel_event() is called with await.

    Example minimal implementation:
        class MyPlugin(BasePlugin):
            @property
            def manifest(self) -> PluginManifest:
                return PluginManifest(
                    id="my_plugin",
                    name="My Plugin",
                    version="1.0.0",
                    min_aeos_version="2.0.0",
                    capabilities=["my_domain.do_thing"],
                )

            async def initialize(self, kernel: AEOSKernel) -> None:
                # Register services, subscribe to events
                kernel.register_service("my_service", MyService(), ["my_domain.do_thing"])

            async def shutdown(self) -> None:
                # Release resources
                ...

            def health(self) -> PluginStatus:
                return PluginStatus.READY

            async def on_kernel_event(self, event: KernelEvent) -> None:
                ...
    """

    # ── Abstract Properties ────────────────────────────────────────────────

    @property
    @abstractmethod
    def manifest(self) -> PluginManifest:
        """
        Return the plugin's manifest.

        Must be available immediately after instantiation — before
        initialize() is called. The Kernel reads the manifest to validate
        the plugin before committing to loading it.

        Returns:
            A fully populated PluginManifest for this plugin.

        Contract:
            - Must be synchronous and non-blocking.
            - Must return the same manifest object on every call (idempotent).
            - Must not raise.
        """
        ...

    # ── Abstract Lifecycle Methods ─────────────────────────────────────────

    @abstractmethod
    async def initialize(self, kernel: AEOSKernel) -> None:
        """
        Initialize the plugin with access to the AEOS Kernel.

        Called by the Kernel during Phase 2 (plugin loading) or when a plugin
        is dynamically registered after boot. The plugin receives a reference
        to the Kernel and uses it to register services, subscribe to events,
        and request resource grants.

        Args:
            kernel: The running AEOSKernel instance. The plugin must store
                this reference for use in event handlers and service calls.

        Contract:
            - Must complete within plugin_init_timeout_seconds (default: 30s).
            - Must call kernel.register_service() for each provided service.
            - Must call kernel.subscribe() for each event topic required.
            - Must NOT call kernel.startup() or kernel.shutdown().
            - Must transition plugin status to READY on success.
            - Must raise PluginInitializationError on failure (not return silently).

        Raises:
            PluginInitializationError: Initialization failed. The Kernel will
                mark this plugin as FAILED and will not call shutdown().
        """
        ...

    async def register_services(self, kernel: AEOSKernel) -> None:
        """
        Register services with the Kernel Service Registry (Phase 3).

        Called by the Kernel during Phase 3 boot, AFTER all plugins have been
        initialized (Phase 2). Override to declare what services this plugin
        provides via kernel.register_service().

        Default implementation is a no-op. Plugins that provide services MUST
        override this method.

        Args:
            kernel: The running AEOSKernel instance.
        """
        pass  # noqa: unnecessary-pass  — intentional no-op default

    @abstractmethod
    async def shutdown(self) -> None:
        """
        Shut down the plugin gracefully and release all held resources.

        Called by the Kernel during plugin unload (kernel.unload_plugin()) or
        during the platform shutdown sequence. After this method returns, the
        Kernel deregisters all services and subscriptions for this plugin.

        Contract:
            - Must complete within plugin_shutdown_timeout_seconds (default: 30s).
            - Must release all external connections and file handles.
            - Must flush any write buffers or in-flight data.
            - Must NOT raise — catch, log, and suppress all exceptions.
            - Must NOT call any Kernel API methods after shutdown() is invoked.
            - Must be idempotent (safe to call multiple times).
        """
        ...

    @abstractmethod
    def health(self) -> PluginStatus:
        """
        Return the current operational health status of this plugin.

        Called by the Kernel during Phase 4 (health verification) and during
        periodic background health polling (default interval: 30 seconds).
        The Kernel uses this to determine whether the plugin is serving requests
        and whether intervention (restart, alert) is needed.

        Returns:
            PluginStatus.READY    — fully operational, all capabilities available.
            PluginStatus.DEGRADED — partially operational, some capabilities limited.
            PluginStatus.FAILED   — non-operational, no capabilities available.

        Contract:
            - Must be synchronous and non-blocking.
            - Must complete within health_check_timeout_seconds (default: 10s).
            - Must never raise — return PluginStatus.FAILED on internal error.
            - Must reflect the actual current state (not a cached value > 5s old).
        """
        ...

    @abstractmethod
    async def on_kernel_event(self, event: KernelEvent) -> None:
        """
        Handle a kernel event delivered to this plugin.

        Called by the Kernel Event Bus for topics this plugin subscribed to
        during initialize(). The Kernel catches, logs, and suppresses any
        exception raised by this method — it does not propagate to the emitter.

        Args:
            event: The KernelEvent delivered to this plugin.

        Contract:
            - Must not raise — catch and handle all exceptions internally.
            - Should complete quickly. Long-running work should be dispatched
              to a background task to avoid blocking the event bus.
            - Must not call kernel.emit() with the same event (infinite loop).
        """
        ...

    # ── Optional Override Methods ──────────────────────────────────────────

    def get_config_schema(self) -> dict[str, Any]:
        """
        Return the JSON Schema for this plugin's configuration.

        Default implementation returns the config_schema from the manifest.
        Override to provide a dynamically generated schema.

        Returns:
            JSON Schema dict (may be empty if no configuration is required).
        """
        return self.manifest.config_schema

    def get_status_detail(self) -> dict[str, Any]:
        """
        Return detailed status information for admin UIs and diagnostics.

        Override to provide plugin-specific status data (e.g., connection pool
        stats, cache hit rates, queue depths).

        Returns:
            Dictionary with plugin-specific status fields. May be empty.
        """
        return {
            "plugin_id": self.manifest.id,
            "version": self.manifest.version,
            "status": self.health().value,
        }

    @property
    def lifecycle_state(self) -> LifecycleState:
        """
        Return the plugin's current lifecycle state.

        Default implementation returns RUNNING if health() is READY,
        FAILED if health() is FAILED, and RUNNING if health() is DEGRADED.

        Plugins with more precise lifecycle tracking should override this
        property to return the actual tracked state.

        Returns:
            The current LifecycleState for this plugin.
        """
        status = self.health()
        if status == PluginStatus.FAILED:
            return LifecycleState.FAILED
        if status in (PluginStatus.READY, PluginStatus.DEGRADED):
            return LifecycleState.RUNNING
        if status == PluginStatus.LOADING:
            return LifecycleState.INITIALIZING
        if status == PluginStatus.UNLOADING:
            return LifecycleState.STOPPING
        return LifecycleState.STOPPED


# ---------------------------------------------------------------------------
# PluginRegistry Abstract Base Class
# ---------------------------------------------------------------------------


class PluginRegistry(ABC):
    """
    Abstract base for the AEOS Kernel Plugin Registry.

    The PluginRegistry maintains the set of loaded plugins, provides lookup
    by ID, and manages the plugin directory scanning during boot Phase 2.

    One PluginRegistry instance is created and owned by the Kernel.
    External code accesses plugins only through the Kernel API
    (kernel.get_plugin(), kernel.list_plugins()) — never through the registry
    directly.

    Concrete implementations:
        - InMemoryPluginRegistry: Dict-backed, single-process (current phase).
        - DistributedPluginRegistry: Cluster-wide plugin tracking (future).
    """

    @abstractmethod
    def register(self, plugin: BasePlugin) -> None:
        """
        Add a plugin to the registry.

        Called by the Kernel after a plugin's initialize() method completes
        successfully. Does NOT call initialize() — the Kernel does that first.

        Args:
            plugin: The initialized BasePlugin instance to register.

        Raises:
            PluginConflictError: A plugin with the same manifest ID is already
                registered. Silent overwriting is not permitted.
        """
        ...

    @abstractmethod
    def deregister(self, plugin_id: str) -> None:
        """
        Remove a plugin from the registry.

        Called by the Kernel after shutdown() completes for the plugin.
        Does NOT call shutdown() — the Kernel does that first.

        Args:
            plugin_id: The manifest ID of the plugin to remove.

        Raises:
            PluginNotFoundError: plugin_id is not in the registry.
        """
        ...

    @abstractmethod
    def get(self, plugin_id: str) -> BasePlugin | None:
        """
        Retrieve a plugin by its manifest ID.

        Args:
            plugin_id: The plugin's manifest ID.

        Returns:
            The BasePlugin instance, or None if not found.
        """
        ...

    @abstractmethod
    def list(self) -> list[PluginManifest]:
        """
        Return manifests for all currently registered plugins.

        Returns:
            List of PluginManifest objects in registration (load) order.
        """
        ...

    @abstractmethod
    def list_by_capability(self, capability: str) -> list[BasePlugin]:
        """
        Return all plugins that declare the given capability in their manifest.

        Args:
            capability: A dot-namespaced capability string (e.g., "rag.query").

        Returns:
            List of BasePlugin instances whose manifests include the capability.
            Empty list if no plugins declare the capability.
        """
        ...

    @abstractmethod
    async def load_from_directory(
        self,
        path: Path,
        required_plugin_ids: list[str] | None = None,
    ) -> list[PluginManifest]:
        """
        Scan a directory for plugin manifests and return discovered manifests.

        This method performs discovery only — it reads and validates manifests
        but does NOT import plugin code or call initialize(). The Kernel
        performs the actual loading after calling this method.

        Walk order: Alphabetical by directory name within the given path.
        Built-in plugins should be loaded before this method is called on
        the external plugin directory.

        Args:
            path: Filesystem path to the plugins directory.
            required_plugin_ids: List of plugin IDs that must be found.
                If any are missing from the discovered manifests, raises
                PluginNotFoundError before returning.

        Returns:
            List of PluginManifest objects for all valid manifests discovered,
            in directory walk order (before dependency sorting).

        Raises:
            PluginNotFoundError: One or more required_plugin_ids were not found
                in the directory.
            PluginDependencyError: A dependency cycle was detected among the
                discovered plugins.
            FileNotFoundError: path does not exist or is not a directory.
        """
        ...

    @abstractmethod
    def resolve_load_order(
        self,
        manifests: list[PluginManifest],
    ) -> list[PluginManifest]:
        """
        Compute the topological load order for a list of plugin manifests.

        Performs a topological sort based on the `dependencies` field of each
        manifest. Plugins with no dependencies come first. A plugin only
        appears after all its dependencies appear.

        Args:
            manifests: List of PluginManifest objects to order.

        Returns:
            The same manifests reordered so that every plugin's dependencies
            appear before it in the list.

        Raises:
            PluginDependencyError: A dependency cycle was detected, or a
                declared dependency is not present in the manifests list.
        """
        ...


# ---------------------------------------------------------------------------
# Plugin Exceptions
# ---------------------------------------------------------------------------


class PluginConflictError(Exception):
    """
    Raised when attempting to register a plugin with an ID that is already
    in use by a different registered plugin.

    Attributes:
        plugin_id: The conflicting plugin ID.
        existing_plugin_version: The version of the already-registered plugin.
        new_plugin_version: The version of the plugin being registered.
    """

    def __init__(
        self,
        plugin_id: str,
        existing_plugin_version: str = "",
        new_plugin_version: str = "",
    ) -> None:
        self.plugin_id = plugin_id
        self.existing_plugin_version = existing_plugin_version
        self.new_plugin_version = new_plugin_version
        super().__init__(
            f"Plugin conflict: '{plugin_id}' is already registered "
            f"(existing: {existing_plugin_version!r}, new: {new_plugin_version!r}). "
            f"Unload the existing plugin before registering a new one."
        )


class PluginDependencyError(Exception):
    """
    Raised when a plugin's declared dependencies cannot be satisfied.

    This covers two cases:
        1. A dependency plugin is not loaded (missing dependency).
        2. Dependency declarations form a cycle (circular dependency).

    Attributes:
        plugin_id: The plugin whose dependencies could not be resolved.
        missing_dependencies: List of dependency plugin IDs that are not loaded.
        cycle: List of plugin IDs forming a dependency cycle (if applicable).
    """

    def __init__(
        self,
        plugin_id: str,
        missing_dependencies: list[str] | None = None,
        cycle: list[str] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.missing_dependencies = missing_dependencies or []
        self.cycle = cycle or []
        if cycle:
            detail = f"Dependency cycle detected: {' → '.join(cycle)}"
        else:
            detail = f"Missing dependencies: {self.missing_dependencies}"
        super().__init__(
            f"Plugin dependency error for '{plugin_id}': {detail}"
        )


class PluginInitializationError(Exception):
    """
    Raised when a plugin's initialize() method fails or times out.

    Attributes:
        plugin_id: The plugin that failed to initialize.
        reason: Human-readable description of the failure.
        original_exception: The underlying exception, if any.
    """

    def __init__(
        self,
        plugin_id: str,
        reason: str,
        original_exception: Exception | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        self.original_exception = original_exception
        super().__init__(
            f"Plugin initialization failed for '{plugin_id}': {reason}"
        )


class PluginNotFoundError(Exception):
    """
    Raised when a plugin lookup by ID returns no result.

    Attributes:
        plugin_id: The ID that was not found in the registry.
    """

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        super().__init__(
            f"Plugin not found: '{plugin_id}'. "
            f"Ensure the plugin is loaded before accessing it."
        )


class PluginShutdownError(Exception):
    """
    Raised when a plugin's shutdown() method fails or times out.

    The Kernel catches this exception during shutdown sequences, logs it,
    and continues shutting down remaining plugins. This exception is
    never re-raised to callers — it is recorded in the kernel health snapshot.

    Attributes:
        plugin_id: The plugin that failed to shut down cleanly.
        reason: Human-readable description of the failure.
        original_exception: The underlying exception, if any.
    """

    def __init__(
        self,
        plugin_id: str,
        reason: str,
        original_exception: Exception | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        self.original_exception = original_exception
        super().__init__(
            f"Plugin shutdown failed for '{plugin_id}': {reason}"
        )


# ---------------------------------------------------------------------------
# Concrete PluginRegistry (in-memory, single-process)
# ---------------------------------------------------------------------------


class InMemoryPluginRegistry(PluginRegistry):
    """
    Default in-memory plugin registry for single-process AEOS deployments.

    Plugins are stored in an ordered dict keyed by plugin_id.
    Registration order matches load order (dependency-resolved topological).
    """

    def __init__(self) -> None:
        self._plugins: dict[str, BasePlugin] = {}

    def register(self, plugin: BasePlugin) -> None:
        pid = plugin.manifest.id
        if pid in self._plugins:
            raise PluginConflictError(plugin_id=pid)
        self._plugins[pid] = plugin

    # Alias expected by kernel.py
    def add_plugin(self, plugin: BasePlugin) -> None:
        self.register(plugin)

    def remove_plugin(self, plugin_id: str) -> None:
        self.deregister(plugin_id)

    def deregister(self, plugin_id: str) -> None:
        if plugin_id not in self._plugins:
            raise PluginNotFoundError(plugin_id=plugin_id)
        del self._plugins[plugin_id]

    def get(self, plugin_id: str) -> "BasePlugin | None":
        return self._plugins.get(plugin_id)

    # Alias expected by kernel.py
    def get_plugin(self, plugin_id: str) -> "BasePlugin | None":
        return self.get(plugin_id)

    def list(self) -> list[PluginManifest]:
        return [p.manifest for p in self._plugins.values()]

    # Alias expected by kernel.py
    def list_plugins(self) -> list[PluginManifest]:
        return self.list()

    def list_by_capability(self, capability: str) -> list["BasePlugin"]:
        return [
            p for p in self._plugins.values()
            if capability in p.manifest.capabilities
        ]

    async def load_from_directory(self, path: Path, required_plugin_ids: list[str] | None = None) -> list[PluginManifest]:
        # External plugin directory scanning is a v3 feature.
        # Built-in plugins are registered programmatically.
        return []

    def resolve_load_order(self, manifests: list[PluginManifest]) -> list[PluginManifest]:
        """Topological sort of plugin manifests by declared dependencies."""
        id_map = {m.id: m for m in manifests}
        visited: set[str] = set()
        result: list[PluginManifest] = []

        def visit(mid: str) -> None:
            if mid in visited:
                return
            visited.add(mid)
            m = id_map.get(mid)
            if m:
                for dep in m.dependencies:
                    visit(dep)
                result.append(m)

        for m in manifests:
            visit(m.id)
        return result
