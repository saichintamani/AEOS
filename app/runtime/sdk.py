"""
Wave 9B.4.9 — SDK & Developer Experience

The AEOS public SDK. Developers write:

    runtime = AEOS()
    runtime.register_agent("my-agent", handler)
    await runtime.submit_workflow(definition)
    result = await runtime.wait_for(workflow_id)

AEOSRuntime wraps the RuntimeCoordinator with a clean ergonomic API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine

from app.runtime_intelligence.contracts import CapabilityProfile, TaskRequirements
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEventType
from app.runtime.workflow_compiler import WorkflowDefinition

logger = logging.getLogger(__name__)

AgentHandler = Callable[[TaskRequirements, Any], Coroutine[Any, Any, dict]]


class AEOSRuntime:
    """
    High-level SDK entry point for AEOS.

    Provides a simplified developer interface over the full runtime stack.
    """

    def __init__(self, coordinator: "RuntimeCoordinator | None" = None) -> None:  # type: ignore[name-defined]
        from app.runtime.coordinator import RuntimeCoordinator
        self._coordinator = coordinator or RuntimeCoordinator()
        self._started = False

    async def __aenter__(self) -> "AEOSRuntime":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        if not self._started:
            await self._coordinator.start()
            self._started = True

    async def stop(self) -> None:
        if self._started:
            await self._coordinator.stop()
            self._started = False

    def register_worker(self, profile: CapabilityProfile) -> None:
        """Register a worker profile with the runtime."""
        asyncio.get_event_loop().create_task(
            self._coordinator.capability_graph.upsert(profile)
        )

    def register_agent(self, agent_id: str, handler: AgentHandler) -> None:
        """Register a named agent handler."""
        self._coordinator.register_agent(agent_id, handler)

    async def submit_workflow(self, definition: WorkflowDefinition) -> str:
        """Compile and submit a workflow. Returns the workflow_id."""
        return await self._coordinator.submit_workflow(definition)

    async def submit_task(self, requirements: TaskRequirements) -> str:
        """Submit a single task. Returns the task_id."""
        return await self._coordinator.submit_task(requirements)

    async def wait_for(self, workflow_id: str, timeout: float = 60.0) -> dict:
        """Wait for a workflow to complete. Returns result dict."""
        return await self._coordinator.wait_for_workflow(workflow_id, timeout)

    def on_event(self, event_type: TelemetryEventType, handler: Any) -> None:
        """Subscribe to a telemetry event type."""
        asyncio.get_event_loop().create_task(
            self._coordinator.telemetry_bus.subscribe(event_type, handler)
        )

    @property
    def telemetry(self) -> TelemetryBus:
        return self._coordinator.telemetry_bus

    @property
    def learning(self):
        return self._coordinator.optimization_loop._learning

    @property
    def knowledge_graph(self):
        return self._coordinator.optimization_loop._kg
