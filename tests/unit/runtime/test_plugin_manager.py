"""Unit tests — PluginManager, PluginRegistry."""

from __future__ import annotations

import pytest

from app.runtime.plugin_manager import (
    LoadedPlugin,
    PluginError,
    PluginManager,
    PluginManifest,
    PluginRegistry,
)


class DummyPlugin:
    def __init__(self):
        self.setup_called = False
        self.teardown_called = False
        self.scheduler = object()

    def setup(self):
        self.setup_called = True

    def teardown(self):
        self.teardown_called = True

    def get_scheduler(self):
        return self.scheduler


class BrokenPlugin:
    def setup(self):
        raise RuntimeError("setup failed")


class TestPluginManager:

    def test_load_plugin(self):
        mgr = PluginManager()
        instance = DummyPlugin()
        manifest = PluginManifest(plugin_id="p1", name="Dummy", provides=["scheduler"])
        loaded = mgr.load(manifest, instance)
        assert loaded.manifest.plugin_id == "p1"
        assert instance.setup_called

    def test_load_calls_setup(self):
        mgr = PluginManager()
        instance = DummyPlugin()
        manifest = PluginManifest(plugin_id="p1", name="Dummy", provides=[])
        mgr.load(manifest, instance)
        assert instance.setup_called

    def test_broken_setup_raises_plugin_error(self):
        mgr = PluginManager()
        manifest = PluginManifest(plugin_id="p2", name="Broken", provides=[])
        with pytest.raises(PluginError, match="setup failed"):
            mgr.load(manifest, BrokenPlugin())

    def test_extensions_collected(self):
        mgr = PluginManager()
        instance = DummyPlugin()
        manifest = PluginManifest(plugin_id="p1", name="Dummy", provides=["scheduler"])
        mgr.load(manifest, instance)
        schedulers = mgr.registry.get_extensions("scheduler")
        assert len(schedulers) == 1
        assert schedulers[0] is instance.scheduler

    def test_duplicate_plugin_overwrites(self):
        mgr = PluginManager()
        manifest = PluginManifest(plugin_id="p1", name="Dummy", provides=[])
        mgr.load(manifest, DummyPlugin())
        mgr.load(manifest, DummyPlugin())   # second load — should not raise
        assert mgr.registry.get("p1") is not None

    def test_unload_calls_teardown(self):
        mgr = PluginManager()
        instance = DummyPlugin()
        manifest = PluginManifest(plugin_id="p1", name="Dummy", provides=[])
        mgr.load(manifest, instance)
        mgr.unload("p1")
        assert instance.teardown_called
        assert mgr.registry.get("p1") is None

    def test_dependency_check_fails_when_missing(self):
        mgr = PluginManager()
        manifest = PluginManifest(
            plugin_id="p2", name="Dependent",
            provides=[],
            dependencies=["p1-required"],
        )
        with pytest.raises(PluginError, match="p1-required"):
            mgr.load(manifest, DummyPlugin())

    def test_dependency_check_passes_when_loaded(self):
        mgr = PluginManager()
        m1 = PluginManifest(plugin_id="p1", name="Base", provides=[])
        mgr.load(m1, DummyPlugin())
        m2 = PluginManifest(plugin_id="p2", name="Dep", provides=[], dependencies=["p1"])
        mgr.load(m2, DummyPlugin())   # should not raise

    def test_missing_plugin_id_raises(self):
        mgr = PluginManager()
        with pytest.raises(PluginError):
            mgr.load(PluginManifest(plugin_id="", name="Bad", provides=[]), DummyPlugin())


class TestPluginRegistry:

    def test_register_and_get(self):
        reg = PluginRegistry()
        manifest = PluginManifest(plugin_id="p1", name="Test", provides=[])
        plugin = LoadedPlugin(manifest=manifest, instance=object())
        reg.register(plugin)
        assert reg.get("p1") is not None

    def test_unregister(self):
        reg = PluginRegistry()
        manifest = PluginManifest(plugin_id="p1", name="Test", provides=[])
        plugin = LoadedPlugin(manifest=manifest, instance=object())
        reg.register(plugin)
        reg.unregister("p1")
        assert reg.get("p1") is None

    def test_all_plugins(self):
        reg = PluginRegistry()
        for i in range(3):
            m = PluginManifest(plugin_id=f"p{i}", name=f"P{i}", provides=[])
            reg.register(LoadedPlugin(manifest=m, instance=object()))
        assert len(reg.all_plugins()) == 3
