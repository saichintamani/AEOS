"""
AEOS Execution Engine

Coordinates all 15 stages of the execution pipeline.
The Engine sits between the Kernel and the Agent Runtime.

Phase 8.3 enhancements:
  - DispatchingExecutor  — typed node dispatch for all node types
  - MetricsCollector     — automatic per-node and per-workflow metrics
  - InMemoryCheckpointStore — checkpoint after each completed node
  - ExecutionEventBus    — fine-grained execution events
  - TraceStore           — full execution trace for replay/debug
  - ExecutionPlanner     — available via engine.planner
  - GraphVisualizer      — available via engine.visualizer

Backward compatibility:
  - engine.run() API is unchanged
  - GovernanceGateResult is still the canonical response type
  - orchestrator.run() adapter unchanged

Usage:
    engine = ExecutionEngine(kernel=kernel)
    result = await engine.run(task="analyze this repo", caller_id="api", trace_id="abc")

    # New DEE APIs:
    print(engine.metrics.summary())
    print(engine.visualizer.to_mermaid(last_graph))
    trace = engine.trace_store.get(result.trace_id)
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

from app.core.logger import get_logger
from app.execution.checkpoint import InMemoryCheckpointStore
from app.execution.events import ExecutionEvent, ExecutionEventBus, ExecutionEventType
from app.execution.executor import DispatchingExecutor
from app.execution.graph import AgentNode, execute_graph
from app.execution.metrics import MetricsCollector
from app.execution.planner import ExecutionPlanner, PlannerConfig
from app.execution.replay import ExecutionTrace, TraceStore
from app.execution.retry import DEFAULT_RETRY_POLICY, RetryEngine
from app.execution.schemas import (
    AggregatedResult,
    ExecutionError,
    ExecutionGraph,
    GovernanceDecision,
    GovernanceGateResult,
    StepResult,
    StepStatus,
    WorkflowState,
    WorkflowStatus,
)
from app.execution.stages import (
    stage1_receive_intent,
    stage2_validate_input,
    stage3_classify_intent,
    stage4_collect_constraints,
    stage5_solve_constraints,
    stage6_decompose_goals,
    stage7_build_goals,
    stage8_plan,
    stage9_compile_graph,
    stage10_workflow_entry,
    stage13_aggregate,
    stage14_reflection_gate,
    stage15_governance_gate,
)
from app.execution.visualization import GraphVisualizer

if TYPE_CHECKING:
    from app.kernel.kernel import AEOSKernel

__all__ = ["ExecutionEngine"]

log = get_logger(__name__)

_MIN_SUCCESS_RATIO = 0.6


class ExecutionEngine:
    """
    15-stage execution pipeline coordinator with full DEE integration.

    DEE components are lazily initialized on first use so the engine
    can be instantiated without a running kernel (e.g., in unit tests).

    Backward-compatible: engine.run() API is unchanged.
    """

    def __init__(self, kernel: "AEOSKernel") -> None:
        self._kernel = kernel

        # ── DEE components ────────────────────────────────────────────────────
        self._metrics = MetricsCollector()
        self._event_bus = ExecutionEventBus()
        self._checkpoint_store = InMemoryCheckpointStore()
        self._trace_store = TraceStore()
        self._retry_engine = RetryEngine()
        self._visualizer = GraphVisualizer()
        self._planner = ExecutionPlanner(kernel=kernel, config=PlannerConfig(enable_retry_wrapping=False))

        # Lazily built — requires kernel to be running
        self._dispatcher: DispatchingExecutor | None = None

        # Track most recently executed graph (for debugging)
        self._last_graph: ExecutionGraph | None = None

    # ── Public DEE properties ──────────────────────────────────────────────────

    @property
    def metrics(self) -> MetricsCollector:
        return self._metrics

    @property
    def event_bus(self) -> ExecutionEventBus:
        return self._event_bus

    @property
    def checkpoint_store(self) -> InMemoryCheckpointStore:
        return self._checkpoint_store

    @property
    def trace_store(self) -> TraceStore:
        return self._trace_store

    @property
    def visualizer(self) -> GraphVisualizer:
        return self._visualizer

    @property
    def planner(self) -> ExecutionPlanner:
        return self._planner

    @property
    def last_graph(self) -> ExecutionGraph | None:
        return self._last_graph

    # ── Main entry point ───────────────────────────────────────────────────────

    async def run(
        self,
        task: str,
        mode: str = "auto",
        caller_id: str = "anonymous",
        request_id: str = "",
        trace_id: str = "",
    ) -> GovernanceGateResult:
        """
        Full 15-stage pipeline execution.

        Returns GovernanceGateResult regardless of success/failure.
        Emits execution events, records metrics, saves checkpoints, traces.
        """
        t_start = time.time()
        task_id = str(uuid.uuid4())
        trace_id = trace_id or str(uuid.uuid4())
        request_id = request_id or str(uuid.uuid4())

        log.info(
            "ExecutionEngine.run started",
            extra={"ctx_task_id": task_id, "ctx_trace_id": trace_id, "ctx_mode": mode},
        )

        # Initialize execution trace
        exec_trace = ExecutionTrace(
            trace_id=trace_id,
            workflow_id=task_id,
            task_description=task[:200],
        )
        exec_trace.add_entry("workflow.started", metadata={"mode": mode, "caller_id": caller_id})

        # Emit workflow started event
        await self._emit(ExecutionEvent(
            event_type=ExecutionEventType.WORKFLOW_STARTED,
            workflow_id=task_id,
            trace_id=trace_id,
            payload={"mode": mode, "caller_id": caller_id},
        ))

        # ── COMPILATION PHASE (Stages 1-9) ─────────────────────────────────

        # Stage 1
        raw = stage1_receive_intent(task, mode, caller_id, request_id)
        if isinstance(raw, ExecutionError):
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            return raw.to_governance_result()
        raw.trace_id = trace_id

        # Stage 2
        validated = stage2_validate_input(raw)
        if isinstance(validated, ExecutionError):
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            return validated.to_governance_result()

        # Stage 3
        intent = stage3_classify_intent(validated)

        # Stage 4
        constraints = stage4_collect_constraints(intent)

        # Stage 5 — use only actual agent IDs
        agent_ids = self._get_available_agent_ids()
        solution = stage5_solve_constraints(
            intent, constraints, agent_ids if agent_ids else ["simple_agent"]
        )
        if isinstance(solution, ExecutionError):
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            return solution.to_governance_result()

        # Stage 6
        goal_set = stage6_decompose_goals(intent, solution)
        if isinstance(goal_set, ExecutionError):
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            return goal_set.to_governance_result()

        # Stage 7
        goal_set = stage7_build_goals(goal_set, constraints)

        # Stage 8
        plan = stage8_plan(goal_set, solution, trace_id=trace_id)

        # Stage 9
        graph = stage9_compile_graph(
            plan, {}, total_timeout_ms=constraints.timeout_seconds * 1000
        )
        if isinstance(graph, ExecutionError):
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            return graph.to_governance_result()

        self._last_graph = graph

        # plan_only mode exits here
        if mode == "plan_only":
            exec_trace.close("plan_only")
            self._trace_store.save(exec_trace)
            return GovernanceGateResult(
                governance_decision=GovernanceDecision.PASS,
                status="plan_only",
                result={
                    "graph_id": graph.graph_id,
                    "nodes": len(graph.nodes),
                    "topological_order": graph.topological_order,
                    "mermaid": self._visualizer.to_mermaid(graph),
                },
                thought=f"Compiled execution graph with {len(graph.nodes)} nodes.",
                trace_id=trace_id,
                quality_score=1.0,
            )

        # ── RUNTIME PHASE (Stages 10-12) ───────────────────────────────────

        # Stage 10
        workflow_state = stage10_workflow_entry(graph, task_id, trace_id)
        workflow_state.workflow_id = task_id

        # Initialize metrics for this workflow
        self._metrics.start_workflow(
            workflow_id=task_id,
            trace_id=trace_id,
            total_nodes=len(graph.nodes),
        )

        # Stage 11: Step Execution (topological parallel dispatch)
        try:
            workflow_state = await asyncio.wait_for(
                execute_graph(
                    graph=graph,
                    workflow_state=workflow_state,
                    execute_node=self._make_traced_dispatch(task_id, exec_trace),
                    max_parallel=solution.effective_max_parallel,
                ),
                timeout=solution.effective_timeout_seconds,
            )
        except asyncio.TimeoutError:
            workflow_state.status = WorkflowStatus.FAILED
            log.warning("Pipeline timeout exceeded", extra={"ctx_trace_id": trace_id})
            exec_trace.add_entry("workflow.timeout", metadata={"timeout_s": solution.effective_timeout_seconds})

        # Stage 12: Result collection
        if not workflow_state.completed_nodes:
            workflow_state.status = WorkflowStatus.FAILED
            failed_result = AggregatedResult(
                content="Task failed: no nodes completed successfully.",
                partial=True,
                partial_success_ratio=0.0,
            )
            reflection = stage14_reflection_gate(
                failed_result, goal_set, revision_count=99
            )
            result = stage15_governance_gate(
                failed_result, reflection, workflow_state,
                task_id=task_id, caller_id=caller_id, trace_id=trace_id, t_start=t_start,
            )
            wall_ms = round((time.time() - t_start) * 1000, 1)
            self._metrics.finish_workflow(task_id, wall_ms, result.quality_score)
            exec_trace.close("failed")
            self._trace_store.save(exec_trace)
            await self._emit(ExecutionEvent(
                event_type=ExecutionEventType.WORKFLOW_FAILED,
                workflow_id=task_id,
                trace_id=trace_id,
                payload={"reason": "no_nodes_completed"},
            ))
            return result

        # ── EVALUATION PHASE (Stages 13-15) ────────────────────────────────

        # Stage 13
        workflow_state.status = WorkflowStatus.AGGREGATING
        aggregated = stage13_aggregate(workflow_state, goal_set)

        # Stage 14 (with revision loop support)
        workflow_state.status = WorkflowStatus.REFLECTING
        reflection = stage14_reflection_gate(
            aggregated=aggregated,
            goal_set=goal_set,
            revision_count=workflow_state.revision_count,
            quality_threshold=constraints.min_quality_threshold,
        )

        # Revision loop (max 2 revisions)
        while (
            reflection.decision.value == "REVISE"
            and workflow_state.revision_count < 2
        ):
            workflow_state.revision_count += 1
            log.info(
                "Revision loop triggered",
                extra={"ctx_round": workflow_state.revision_count, "ctx_quality": reflection.overall_quality_score},
            )
            exec_trace.add_entry("workflow.revision", metadata={
                "round": workflow_state.revision_count,
                "quality": reflection.overall_quality_score,
                "targets": reflection.revision_targets,
            })

            dispatch = self._make_traced_dispatch(task_id, exec_trace)
            for node_id in reflection.revision_targets:
                node = next((n for n in graph.nodes if n.node_id == node_id), None)
                if node:
                    workflow_state.completed_nodes.discard(node_id)
                    revised_result = await dispatch(node, workflow_state)
                    workflow_state.step_results[node_id] = revised_result
                    if revised_result.status == StepStatus.COMPLETED:
                        workflow_state.completed_nodes.add(node_id)

            aggregated = stage13_aggregate(workflow_state, goal_set)
            reflection = stage14_reflection_gate(
                aggregated, goal_set,
                revision_count=workflow_state.revision_count,
            )

        # Stage 15
        workflow_state.status = WorkflowStatus.GOVERNANCE
        result = stage15_governance_gate(
            aggregated=aggregated,
            reflection=reflection,
            workflow_state=workflow_state,
            task_id=task_id,
            caller_id=caller_id,
            trace_id=trace_id,
            t_start=t_start,
        )
        workflow_state.status = WorkflowStatus.COMPLETED

        # Finalize metrics
        wall_ms = round((time.time() - t_start) * 1000, 1)
        self._metrics.finish_workflow(task_id, wall_ms, result.quality_score)

        # Save trace
        exec_trace.close("completed")
        exec_trace.add_entry("workflow.completed", metadata={
            "quality_score": result.quality_score,
            "wall_time_ms": wall_ms,
            "agents_used": result.agents_used,
        })
        self._trace_store.save(exec_trace)

        await self._emit(ExecutionEvent(
            event_type=ExecutionEventType.WORKFLOW_COMPLETED,
            workflow_id=task_id,
            trace_id=trace_id,
            payload={
                "status": result.status,
                "quality_score": result.quality_score,
                "wall_time_ms": wall_ms,
            },
        ))

        log.info(
            "ExecutionEngine.run complete",
            extra={
                "ctx_status": result.status,
                "ctx_quality": result.quality_score,
                "ctx_latency_ms": result.latency_ms,
                "ctx_trace_id": trace_id,
            },
        )
        return result

    # ── Traced dispatch ────────────────────────────────────────────────────────

    def _make_traced_dispatch(
        self,
        workflow_id: str,
        exec_trace: ExecutionTrace,
    ):
        """
        Build a node dispatch callable that:
        1. Emits node events
        2. Records metrics
        3. Saves checkpoints after completion
        4. Appends to the execution trace
        """
        async def _dispatch(node: Any, workflow_state: WorkflowState) -> StepResult:
            exec_trace.add_entry("node.started", node_id=node.node_id)
            await self._emit(ExecutionEvent(
                event_type=ExecutionEventType.NODE_STARTED,
                workflow_id=workflow_id,
                node_id=node.node_id,
                trace_id=workflow_state.trace_id,
                payload={"node_type": node.node_type},
            ))

            result = await self._dispatch_node(node, workflow_state)

            # Record metrics
            self._metrics.record_step(workflow_id, result)

            # Emit node event
            event_type = (
                ExecutionEventType.NODE_COMPLETED
                if result.status == StepStatus.COMPLETED
                else ExecutionEventType.NODE_TIMED_OUT
                if result.status == StepStatus.TIMED_OUT
                else ExecutionEventType.NODE_FAILED
            )
            exec_trace.add_entry(
                f"node.{'completed' if result.status == StepStatus.COMPLETED else 'failed'}",
                node_id=node.node_id,
                step_result=result,
            )
            await self._emit(ExecutionEvent(
                event_type=event_type,
                workflow_id=workflow_id,
                node_id=node.node_id,
                trace_id=workflow_state.trace_id,
                payload={
                    "status": result.status.value,
                    "latency_ms": result.latency_ms,
                    "agent_id": result.agent_id,
                },
            ))

            # Checkpoint after completed node
            if result.status == StepStatus.COMPLETED:
                try:
                    await self._checkpoint_store.save(
                        workflow_state,
                        trigger_node_id=node.node_id,
                    )
                    await self._emit(ExecutionEvent(
                        event_type=ExecutionEventType.CHECKPOINT_SAVED,
                        workflow_id=workflow_id,
                        node_id=node.node_id,
                        trace_id=workflow_state.trace_id,
                    ))
                except Exception as cp_exc:
                    log.warning(
                        "Checkpoint save failed (non-fatal)",
                        extra={"ctx_node_id": node.node_id, "ctx_error": str(cp_exc)},
                    )

            return result

        return _dispatch

    # ── Node dispatch ──────────────────────────────────────────────────────────

    async def _dispatch_node(self, node: Any, workflow_state: WorkflowState) -> StepResult:
        """
        Route a node to the DispatchingExecutor (DEE) or fall back to
        the legacy agent-only dispatch for backward compatibility.
        """
        dispatcher = self._get_dispatcher()
        if dispatcher is not None:
            try:
                return await dispatcher.execute(node, workflow_state)
            except Exception as exc:
                log.warning(
                    "DispatchingExecutor failed — falling back to legacy dispatch",
                    extra={"ctx_node_id": node.node_id, "ctx_error": str(exc)},
                )

        # Legacy fallback
        return await self._legacy_dispatch(node, workflow_state)

    def _get_dispatcher(self) -> DispatchingExecutor | None:
        """Lazily build the DispatchingExecutor."""
        if self._dispatcher is None:
            try:
                self._dispatcher = DispatchingExecutor.build(self._kernel)
            except Exception as exc:
                log.warning(
                    "Could not build DispatchingExecutor",
                    extra={"ctx_error": str(exc)},
                )
        return self._dispatcher

    async def _legacy_dispatch(self, node: Any, workflow_state: WorkflowState) -> StepResult:
        """
        Original dispatch logic — kept for backward compatibility.
        Used as fallback if DispatchingExecutor fails.
        """
        t_node_start = time.time()

        if isinstance(node, AgentNode):
            return await self._dispatch_agent_node(node, workflow_state, t_node_start)

        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value={"message": f"Node type {node.node_type} executed (legacy fallback)"},
            latency_ms=round((time.time() - t_node_start) * 1000, 1),
        )

    async def _dispatch_agent_node(
        self,
        node: AgentNode,
        workflow_state: WorkflowState,
        t_start: float,
    ) -> StepResult:
        """Resolve and run an agent for a given AgentNode (legacy path)."""
        agent = self._resolve_agent(node.agent_type)

        if agent is None:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"No agent available for type '{node.agent_type}'",
            )

        context = {
            "task_id": workflow_state.task_id,
            "trace_id": workflow_state.trace_id,
            "mode": "execution_engine",
            "upstream_results": {
                nid: sr.value for nid, sr in workflow_state.step_results.items()
                if isinstance(sr, StepResult) and sr.status == StepStatus.COMPLETED
            },
        }

        try:
            response = await asyncio.wait_for(
                agent.run(node.task_description, context),
                timeout=node.timeout_ms / 1000.0,
            )
            latency = round((time.time() - t_start) * 1000, 1)
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED if response.status == "success" else StepStatus.FAILED,
                value=response.result,
                error=response.error,
                agent_id=response.agent_id,
                latency_ms=latency,
                confidence=0.85,
            )
        except asyncio.TimeoutError:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.TIMED_OUT,
                error=f"Agent timeout after {node.timeout_ms}ms",
                latency_ms=node.timeout_ms,
            )
        except Exception as exc:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=str(exc),
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )

    def _resolve_agent(self, agent_type: str) -> Any:
        """Find an agent instance by type name."""
        try:
            return self._kernel.get_service(agent_type)
        except Exception:
            pass
        try:
            orch_service = self._kernel.get_service("orchestrator")
            if hasattr(orch_service, "_registry"):
                return orch_service._registry.get(agent_type)
        except Exception:
            pass
        return None

    def _get_available_agent_ids(self) -> list[str]:
        """Return all agent IDs currently registered via the orchestrator."""
        try:
            orch = self._kernel.get_service("orchestrator")
            if hasattr(orch, "_registry"):
                return list(orch._registry.keys())
        except Exception:
            pass
        return []

    # ── Event helpers ──────────────────────────────────────────────────────────

    async def _emit(self, event: ExecutionEvent) -> None:
        """Emit an event — never raises."""
        try:
            await self._event_bus.emit(event)
        except Exception as exc:
            log.warning(
                "ExecutionEventBus emit failed",
                extra={"ctx_event": event.event_type.value, "ctx_error": str(exc)},
            )

    # ── Introspection ──────────────────────────────────────────────────────────

    def introspect(self) -> dict[str, Any]:
        """Full DEE runtime state for debug endpoints."""
        return {
            "metrics": self._metrics.summary(),
            "events": self._event_bus.summarize(),
            "checkpoints": self._checkpoint_store.summarize(),
            "traces": {"stored": self._trace_store.count()},
            "retry_engine": self._retry_engine.summarize(),
            "last_graph_id": self._last_graph.graph_id if self._last_graph else None,
            "last_graph_nodes": len(self._last_graph.nodes) if self._last_graph else 0,
        }
