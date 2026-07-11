"""
Wave 9B.4.8 — Plugin Runtime

Extension system for AEOS. Plugins can contribute:
  - schedulers
  - planners
  - optimizers
  - governance policies
  - transports
  - reasoning modules

PluginManifest   — declares what a plugin provides
PluginRegistry   — stores and retrieves loaded plugins
PluginManager    — loads, validates, and activates plugins
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


class PluginError(RuntimeError):
    """Raised when a plugin fails to load or activate."""


@dataclass
class PluginManifest:
    plugin_id: str
    name: str
    version: str = "0.1.0"
    provides: list[str] = field(default_factory=list)   # e.g. ["scheduler", "policy"]
    author: str = ""
    description: str = ""
    dependencies: list[str] = field(default_factory=list)   # other plugin_ids required first


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    instance: Any                          # the actual plugin object
    extensions: dict[str, Any] = field(default_factory=dict)  # extension_type → object


class PluginRegistry:
    """In-memory registry of loaded plugins."""

    def __init__(self) -> None:
        self._plugins: dict[str, LoadedPlugin] = {}
        self._extensions: dict[str, list[Any]] = {}   # extension_type → [objects]

    def register(self, plugin: LoadedPlugin) -> None:
        self._plugins[plugin.manifest.plugin_id] = plugin
        for ext_type, obj in plugin.extensions.items():
            if ext_type not in self._extensions:
                self._extensions[ext_type] = []
            self._extensions[ext_type].append(obj)

    def get(self, plugin_id: str) -> LoadedPlugin | None:
        return self._plugins.get(plugin_id)

    def all_plugins(self) -> list[LoadedPlugin]:
        return list(self._plugins.values())

    def get_extensions(self, extension_type: str) -> list[Any]:
        return list(self._extensions.get(extension_type, []))

    def unregister(self, plugin_id: str) -> None:
        plugin = self._plugins.pop(plugin_id, None)
        if plugin:
            for ext_type in plugin.extensions:
                self._extensions[ext_type] = [
                    obj for obj in self._extensions.get(ext_type, [])
                    if obj not in plugin.extensions.values()
                ]


class PluginManager:
    """
    Loads, validates, and activates plugins.

    Loading protocol:
      1. Validate manifest (required fields, dependency resolution)
      2. Call plugin.setup() if available
      3. Register in PluginRegistry
      4. Emit PLUGIN_LOADED telemetry
    """

    def __init__(
        self,
        registry: PluginRegistry | None = None,
        telemetry_bus: TelemetryBus | None = None,
    ) -> None:
        self._registry = registry or PluginRegistry()
        self._bus = telemetry_bus

    @property
    def registry(self) -> PluginRegistry:
        return self._registry

    def load(self, manifest: PluginManifest, instance: Any) -> LoadedPlugin:
        """Load a plugin from a manifest and its instance object."""
        self._validate_manifest(manifest)
        self._check_dependencies(manifest)

        # Call setup if present
        if hasattr(instance, "setup") and callable(instance.setup):
            try:
                instance.setup()
            except Exception as exc:
                self._emit_failure(manifest, str(exc))
                raise PluginError(f"Plugin '{manifest.plugin_id}' setup failed: {exc}") from exc

        # Collect extensions
        extensions: dict[str, Any] = {}
        for ext_type in manifest.provides:
            getter = f"get_{ext_type}"
            if hasattr(instance, getter):
                extensions[ext_type] = getattr(instance, getter)()
            elif hasattr(instance, ext_type):
                extensions[ext_type] = getattr(instance, ext_type)

        plugin = LoadedPlugin(manifest=manifest, instance=instance, extensions=extensions)
        self._registry.register(plugin)

        logger.info(
            "PluginManager: loaded '%s' v%s — provides: %s",
            manifest.name, manifest.version, manifest.provides,
        )
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.PLUGIN_LOADED,
                source="PluginManager",
                payload={
                    "plugin_id": manifest.plugin_id,
                    "name": manifest.name,
                    "provides": manifest.provides,
                },
            ))
        return plugin

    def unload(self, plugin_id: str) -> None:
        plugin = self._registry.get(plugin_id)
        if not plugin:
            return
        if hasattr(plugin.instance, "teardown") and callable(plugin.instance.teardown):
            try:
                plugin.instance.teardown()
            except Exception:
                logger.exception("PluginManager: teardown error for '%s'", plugin_id)
        self._registry.unregister(plugin_id)
        logger.info("PluginManager: unloaded '%s'", plugin_id)

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate_manifest(self, manifest: PluginManifest) -> None:
        if not manifest.plugin_id:
            raise PluginError("Manifest missing plugin_id")
        if not manifest.name:
            raise PluginError("Manifest missing name")

    def _check_dependencies(self, manifest: PluginManifest) -> None:
        for dep_id in manifest.dependencies:
            if not self._registry.get(dep_id):
                raise PluginError(
                    f"Plugin '{manifest.plugin_id}' requires '{dep_id}' which is not loaded"
                )

    def _emit_failure(self, manifest: PluginManifest, error: str) -> None:
        if self._bus:
            self._bus.emit(TelemetryEvent(
                event_type=TelemetryEventType.PLUGIN_FAILED,
                source="PluginManager",
                payload={"plugin_id": manifest.plugin_id, "error": error},
            ))
