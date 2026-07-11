"""
Wave 9B.4.1 — Runtime Coordinator

The heart of AEOS. Coordinates every execution lifecycle:

  submit_workflow()
    → WorkflowCompiler.compile()
    → ExecutionPlanner.plan()
    → per-node: PolicyRuntime.evaluate()
    → per-node: DecisionEngine.decide()
    → dispatch task to worker
    → ExecutionMonitor tracks state
    → on complete: OptimizationLoop.ingest()
    → on failure: SelfHealingRuntime.heal()

Every lifecycle event is published on the TelemetryBus.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine

from app.runtime_intelligence.capability_graph import CapabilityGraph
from app.runtime_intelligence.contracts import (
    ExecutionRecord,
    TaskRequirements,
)
from app.runtime_intelligence.decision_engine import ExpectedUtilityDecisionEngine
from app.runtime_intelligence.execution_planner import ExecutionPlanner
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime.adaptive_resource_manager import AdaptiveResourceManager
from app.runtime.execution_monitor import ExecutionMonitor, LiveState, TaskState
from app.runtime.optimization_loop import AutonomousOptimizationLoop
from app.runtime.policy_runtime import PolicyRuntime
from app.runtime.self_healing import FailureContext, SelfHealingRuntime
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType
from app.runtime.workflow_compiler import WorkflowCompiler, WorkflowDefinition

logger = logging.getLogger(__name__)

AgentHandler = Callable[[TaskRequirements, Any], Coroutine[Any, Any, dict]]


class RuntimeCoordinator:
    """
    Central orchestration hub.

    Wires together all Phase 9B subsystems and drives the execution lifecycle.
    """

    def __init__(
        self,
        learning_engine: DefaultLearningEngine | None = None,
        telemetry_bus: TelemetryBus | None = None,
    ) -> None:
        self._telemetry = telemetry_bus or TelemetryBus()
        self._learning = learning_engine or DefaultLearningEngine()

        self._capability_graph = CapabilityGraph()
        self._decision_engine = ExpectedUtilityDecisionEngine(self._learning)
        self._workflow_compiler = WorkflowCompiler(self._telemetry)
        self._execution_planner = ExecutionPlanner(self._decision_engine)
        self._policy_runtime = PolicyRuntime(telemetry_bus=self._telemetry)
        self._self_healing = SelfHealingRuntime(self._learning, self._telemetry)
        self._optimization_loop = AutonomousOptimizationLoop(
            self._learning, telemetry_bus=self._telemetry
        )
        self._resource_manager = AdaptiveResourceManager(self._telemetry)
        self._monitor = ExecutionMonitor()

        self._agents: dict[str, AgentHandler] = {}
        self._running = False

    # ── Public properties ─────────────────────────────────────────────────────

    @property
    def telemetry_bus(self) -> TelemetryBus:
        return self._telemetry

    @property
    def capability_graph(self) -> CapabilityGraph:
        return self._capability_graph

    @property
    def optimization_loop(self) -> AutonomousOptimizationLoop:
        return self._optimization_loop

    @property
    def monitor(self) -> ExecutionMonitor:
        return self._monitor

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        await self._optimization_loop.start()
        logger.info("RuntimeCoordinator: started")

    async def stop(self) -> None:
        self._running = False
        await self._optimization_loop.stop()
        logger.info("RuntimeCoordinator: stopped")

    def register_agent(self, agent_id: str, handler: AgentHandler) -> None:
        self._agents[agent_id] = handler

    # ── Submission ────────────────────────────────────────────────────────────

    async def submit_workflow(self, definition: WorkflowDefinition) -> str:
        graph = self._workflow_compiler.compile(definition)
        profiles = await self._capability_graph.healthy_profiles()
        plan = await self._execution_planner.plan(
            list(graph.nodes[tid].requirements for tid in graph.nodes),
            profiles,
        )

        # Dispatch all tasks asynchronously
        for tid, decision in plan.decisions.items():
            req = graph.nodes[tid].requirements
            asyncio.create_task(
                self._dispatch_task(req, decision.worker_id),
                name=f"task-{tid}",
            )

        self._telemetry.emit(TelemetryEvent(
            event_type=TelemetryEventType.TASK_SUBMITTED,
            source="RuntimeCoordinator",
            payload={
                "workflow_id": definition.workflow_id,
                "task_count": len(plan.decisions),
                "feasible": plan.is_feasible,
            },
            correlation_id=definition.workflow_id,
        ))
        return definition.workflow_id

    async def submit_task(self, requirements: TaskRequirements) -> str:
        profiles = await self._capability_graph.healthy_profiles()
        decision = await self._decision_engine.decide(requirements, profiles)
        asyncio.create_task(
            self._dispatch_task(requirements, decision.worker_id),
            name=f"task-{requirements.task_id}",
        )
        return requirements.task_id

    async def wait_for_workflow(self, workflow_id: str, timeout: float = 60.0) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if await self._monitor.is_workflow_done(workflow_id):
                return await self._monitor.workflow_result(workflow_id)
            await asyncio.sleep(0.1)
        # Return partial result on timeout
        return await self._monitor.workflow_result(workflow_id)

    # ── Dispatch ──────────────────────────────────────────────────────────────

    async def _dispatch_task(
        self, requirements: TaskRequirements, worker_id: str
    ) -> None:
        task_id = requirements.task_id
        workflow_id = requirements.workflow_id

        state = TaskState(
            task_id=task_id,
            workflow_id=workflow_id,
            worker_id=worker_id,
            state=LiveState.PENDING,
        )
        await self._monitor.track(state)

        # Policy check
        profiles = await self._capability_graph.healthy_profiles()
        worker_profile = next((p for p in profiles if p.worker_id == worker_id), None)
        if worker_profile:
            verdict = await self._policy_runtime.evaluate(worker_profile, requirements)
            if not verdict.allowed:
                await self._monitor.update(task_id, state=LiveState.FAILED,
                                           error=f"policy blocked: {verdict.reason}")
                return

        self._telemetry.emit(TelemetryEvent(
            event_type=TelemetryEventType.TASK_STARTED,
            source="RuntimeCoordinator",
            payload={"task_id": task_id, "worker_id": worker_id},
            correlation_id=task_id,
            worker_id=worker_id,
        ))
        await self._monitor.update(
            task_id,
            state=LiveState.RUNNING,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # Execute
        handler = self._agents.get(requirements.task_type)
        result: dict = {}
        error: str = ""
        success = False
        start_mono = asyncio.get_event_loop().time()

        try:
            if handler:
                result = await handler(requirements, None)
            success = True
        except Exception as exc:
            error = str(exc)
            logger.exception("RuntimeCoordinator: task %s failed: %s", task_id, exc)

        elapsed_ms = (asyncio.get_event_loop().time() - start_mono) * 1000.0

        if success:
            await self._monitor.update(
                task_id,
                state=LiveState.COMPLETED,
                result=result,
                completed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._telemetry.emit(TelemetryEvent(
                event_type=TelemetryEventType.TASK_COMPLETED,
                source="RuntimeCoordinator",
                payload={"task_id": task_id, "worker_id": worker_id},
                correlation_id=task_id,
                worker_id=worker_id,
            ))
        else:
            await self._monitor.update(task_id, state=LiveState.FAILED, error=error)
            self._telemetry.emit(TelemetryEvent(
                event_type=TelemetryEventType.TASK_FAILED,
                source="RuntimeCoordinator",
                payload={"task_id": task_id, "error": error, "worker_id": worker_id},
                correlation_id=task_id,
                worker_id=worker_id,
            ))
            # Trigger self-healing
            failure = FailureContext(
                task_id=task_id,
                worker_id=worker_id,
                task_type=requirements.task_type,
                workflow_id=workflow_id,
                error_type=type(error).__name__ if error else "UnknownError",
            )
            healing_action = await self._self_healing.heal(failure, profiles)
            logger.info("RuntimeCoordinator: healing action=%s for task %s",
                        healing_action.strategy, task_id)

        # Ingest record for learning
        record = ExecutionRecord(
            task_id=task_id,
            worker_id=worker_id,
            task_type=requirements.task_type,
            workflow_id=workflow_id,
            success=success,
            failed=not success,
            error_type="" if success else "ExecutionError",
            execution_time_ms=elapsed_ms,
            latency_ms=elapsed_ms,
        )
        await self._optimization_loop.ingest(record)
