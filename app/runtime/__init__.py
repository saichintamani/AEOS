"""
app.runtime — Autonomous Orchestration Runtime (Phase 9B.4)

The heart of AEOS: connects all subsystems into a coherent orchestration platform.
"""

from app.runtime.adaptive_resource_manager import (
    AdaptiveResourceManager,
    ResourceAction,
    ResourceDecision,
    ResourceSnapshot,
)
from app.runtime.coordinator import RuntimeCoordinator
from app.runtime.execution_monitor import ExecutionMonitor, LiveState, TaskState
from app.runtime.lifecycle_controller import LifecycleController, LifecycleState
from app.runtime.optimization_loop import AutonomousOptimizationLoop
from app.runtime.plugin_manager import (
    LoadedPlugin,
    PluginError,
    PluginManager,
    PluginManifest,
    PluginRegistry,
)
from app.runtime.policy_runtime import (
    PolicyDefinition,
    PolicyOverride,
    PolicyRegistry,
    PolicyRuntime,
    PolicyVerdict,
)
from app.runtime.runtime_profiler import ProfilerReport, RuntimeProfiler
from app.runtime.sdk import AEOSRuntime
from app.runtime.self_healing import (
    FailureContext,
    HealingAction,
    RecoveryStrategy,
    SelfHealingRuntime,
)
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType
from app.runtime.workflow_compiler import (
    CompilationError,
    TaskSpec,
    WorkflowCompiler,
    WorkflowDefinition,
)

__all__ = [
    # Coordinator (9B.4.1)
    "RuntimeCoordinator",
    # Workflow Compiler (9B.4.2)
    "CompilationError", "TaskSpec", "WorkflowCompiler", "WorkflowDefinition",
    # Resource Manager (9B.4.3)
    "AdaptiveResourceManager", "ResourceAction", "ResourceDecision", "ResourceSnapshot",
    # Self-Healing (9B.4.4)
    "FailureContext", "HealingAction", "RecoveryStrategy", "SelfHealingRuntime",
    # Policy Runtime (9B.4.5)
    "PolicyDefinition", "PolicyOverride", "PolicyRegistry", "PolicyRuntime", "PolicyVerdict",
    # Optimization Loop (9B.4.6)
    "AutonomousOptimizationLoop",
    # Telemetry Bus (9B.4.7)
    "TelemetryBus", "TelemetryEvent", "TelemetryEventType",
    # Plugin Runtime (9B.4.8)
    "LoadedPlugin", "PluginError", "PluginManager", "PluginManifest", "PluginRegistry",
    # SDK (9B.4.9)
    "AEOSRuntime",
    # Production Validation (9B.4.10)
    "ProfilerReport", "RuntimeProfiler",
    # Support
    "ExecutionMonitor", "LiveState", "TaskState",
    "LifecycleController", "LifecycleState",
]
