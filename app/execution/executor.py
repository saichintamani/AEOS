"""
AEOS Distributed Execution Engine — Node Executors

NodeExecutor ABC + concrete implementations for each node type.
DispatchingExecutor routes to the correct executor based on node type.

Architecture:
  DispatchingExecutor
    ├── AgentNodeExecutor       → dispatches to a CognitiveAgent
    ├── CapabilityNodeExecutor  → dispatches by kernel capability
    ├── ToolNodeExecutor        → invokes a registered tool
    ├── ConditionalNodeExecutor → evaluates condition, selects branch
    ├── ParallelNodeExecutor    → spawns child nodes concurrently
    ├── RetryNodeExecutor       → wraps another node with RetryPolicy
    ├── LoopNodeExecutor        → iterates over a collection
    ├── ApprovalNodeExecutor    → auto-approves or blocks pending human approval
    └── HumanInputNodeExecutor  → injects a placeholder (async wait in future)

The CompositeExecutor (alias for DispatchingExecutor) is what the
ExecutionEngine should hold as its single executor reference.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app.core.logger import get_logger
from app.execution.conditions import ConditionContext, evaluate_condition
from app.execution.graph import (
    AgentNode,
    ConditionalNode,
    GraphNode,
    JoinNode,
    ParallelNode,
    ToolNode,
)
from app.execution.node import (
    ApprovalNode,
    CapabilityNode,
    HumanInputNode,
    LoopNode,
    MergeNode,
    RetryNode,
)
from app.execution.retry import RetryEngine, RetryPolicy
from app.execution.schemas import StepResult, StepStatus, WorkflowState

if TYPE_CHECKING:
    from app.kernel.kernel import AEOSKernel

__all__ = [
    "BaseNodeExecutor",
    "AgentNodeExecutor",
    "CapabilityNodeExecutor",
    "ToolNodeExecutor",
    "ConditionalNodeExecutor",
    "ParallelNodeExecutor",
    "RetryNodeExecutor",
    "LoopNodeExecutor",
    "ApprovalNodeExecutor",
    "HumanInputNodeExecutor",
    "JoinNodeExecutor",
    "DispatchingExecutor",
]

log = get_logger(__name__)


# ── Base ──────────────────────────────────────────────────────────────────────

class BaseNodeExecutor(ABC):
    """
    Abstract base for all node executors.

    Each implementation handles one or more node types.
    """

    @abstractmethod
    def can_handle(self, node: GraphNode) -> bool:
        """Return True if this executor can handle the given node."""

    @abstractmethod
    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        """Execute the node and return a StepResult."""


# ── Agent Node Executor ────────────────────────────────────────────────────────

class AgentNodeExecutor(BaseNodeExecutor):
    """Dispatches AgentNode to a registered agent via kernel service registry."""

    def __init__(self, kernel: "AEOSKernel") -> None:
        self._kernel = kernel

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, AgentNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, AgentNode)
        t_start = time.time()

        agent = self._resolve_agent(node.agent_type)
        if agent is None:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"No agent available for type={node.agent_type!r}",
                latency_ms=0.0,
            )

        context: dict[str, Any] = {
            "task_id": state.task_id,
            "trace_id": state.trace_id,
            "mode": "execution_engine",
            "upstream_results": {
                nid: sr.value
                for nid, sr in state.step_results.items()
                if isinstance(sr, StepResult) and sr.status == StepStatus.COMPLETED
            },
        }
        if node.context_keys:
            context["requested_keys"] = node.context_keys

        try:
            timeout_s = node.timeout_ms / 1000.0
            response = await asyncio.wait_for(
                agent.run(node.task_description, context),
                timeout=timeout_s,
            )
            latency_ms = round((time.time() - t_start) * 1000, 1)
            is_success = getattr(response, "status", "unknown") == "success"
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED if is_success else StepStatus.FAILED,
                value=getattr(response, "result", None),
                error=getattr(response, "error", ""),
                agent_id=getattr(response, "agent_id", ""),
                latency_ms=latency_ms,
                confidence=0.85,
            )
        except asyncio.TimeoutError:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.TIMED_OUT,
                error=f"Agent timeout after {node.timeout_ms:.0f}ms",
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
        try:
            return self._kernel.get_service(agent_type)
        except Exception:
            pass
        try:
            orch = self._kernel.get_service("orchestrator")
            if hasattr(orch, "_registry"):
                return orch._registry.get(agent_type)
        except Exception:
            pass
        return None


# ── Capability Node Executor ───────────────────────────────────────────────────

class CapabilityNodeExecutor(BaseNodeExecutor):
    """Dispatches CapabilityNode by capability name rather than agent type."""

    def __init__(self, kernel: "AEOSKernel") -> None:
        self._kernel = kernel

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, CapabilityNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, CapabilityNode)
        t_start = time.time()

        services = self._kernel.find_by_capability(node.capability)
        if not services:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"No service found with capability={node.capability!r}",
            )

        # Use first available service
        service = services[0]
        try:
            timeout_s = node.timeout_ms / 1000.0
            result = await asyncio.wait_for(
                service.run(node.task_description, {"trace_id": state.trace_id}),
                timeout=timeout_s,
            )
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED,
                value=getattr(result, "result", result),
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )
        except asyncio.TimeoutError:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.TIMED_OUT,
                error=f"Capability node timeout after {node.timeout_ms:.0f}ms",
                latency_ms=node.timeout_ms,
            )
        except Exception as exc:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=str(exc),
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )


# ── Tool Node Executor ─────────────────────────────────────────────────────────

class ToolNodeExecutor(BaseNodeExecutor):
    """Invokes a registered tool directly by tool_id."""

    def __init__(self, kernel: "AEOSKernel") -> None:
        self._kernel = kernel

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, ToolNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, ToolNode)
        t_start = time.time()
        try:
            tool = self._kernel.get_service(node.tool_id)
        except Exception:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"Tool not found: {node.tool_id!r}",
            )
        try:
            result = await asyncio.wait_for(
                tool.invoke(**node.tool_params),
                timeout=node.timeout_ms / 1000.0,
            )
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED,
                value=result,
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )
        except asyncio.TimeoutError:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.TIMED_OUT,
                error=f"Tool timeout after {node.timeout_ms:.0f}ms",
            )
        except Exception as exc:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=str(exc),
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )


# ── Conditional Node Executor ──────────────────────────────────────────────────

class ConditionalNodeExecutor(BaseNodeExecutor):
    """
    Evaluates condition_expr against workflow state.

    The result is stored as {"branch": "true" | "false", "condition": bool}.
    The downstream graph must have edges with condition_value set accordingly.
    """

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, ConditionalNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, ConditionalNode)
        t_start = time.time()

        upstream: dict[str, Any] = {
            nid: sr.value if isinstance(sr, StepResult) else sr
            for nid, sr in state.step_results.items()
        }
        ctx = ConditionContext(
            upstream=upstream,
            workflow={"task_id": state.task_id, "trace_id": state.trace_id},
        )

        try:
            branch_taken = evaluate_condition(node.condition_expr, ctx.to_namespace())
        except Exception as exc:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"Condition evaluation error: {exc}",
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )

        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value={"branch": "true" if branch_taken else "false", "condition": branch_taken},
            latency_ms=round((time.time() - t_start) * 1000, 1),
        )


# ── Parallel Node Executor ─────────────────────────────────────────────────────

class ParallelNodeExecutor(BaseNodeExecutor):
    """
    ParallelNode marks children for concurrent execution.
    The node itself is a coordination point — it completes immediately.
    Actual child execution is handled by execute_graph's batch dispatch.
    """

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, ParallelNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, ParallelNode)
        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value={"parallel_children": node.child_node_ids},
            latency_ms=0.0,
        )


# ── Join Node Executor ─────────────────────────────────────────────────────────

class JoinNodeExecutor(BaseNodeExecutor):
    """Merges results from multiple upstream nodes."""

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, (JoinNode, MergeNode))

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        t_start = time.time()
        input_ids: list[str] = getattr(node, "input_node_ids", [])
        strategy: str = getattr(node, "join_strategy", "all")

        upstream_results = {
            nid: state.step_results.get(nid)
            for nid in input_ids
            if nid in state.step_results
        }

        completed = {
            nid: sr for nid, sr in upstream_results.items()
            if isinstance(sr, StepResult) and sr.status == StepStatus.COMPLETED
        }

        if strategy == "all" and len(completed) < len(input_ids):
            failed_ids = [nid for nid in input_ids if nid not in completed]
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"Join(all) failed: nodes {failed_ids} did not complete",
                latency_ms=round((time.time() - t_start) * 1000, 1),
            )

        if strategy == "first":
            first = next(iter(completed.values()), None)
            merged = first.value if first else None
        elif strategy == "best_quality":
            best = max(
                completed.values(),
                key=lambda sr: getattr(sr, "confidence", 0.0),
                default=None,
            )
            merged = best.value if best else None
        else:
            # Default: merge all values into a list
            merged = {nid: sr.value for nid, sr in completed.items()}

        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value=merged,
            latency_ms=round((time.time() - t_start) * 1000, 1),
            confidence=sum(getattr(sr, "confidence", 1.0) for sr in completed.values()) / max(len(completed), 1),
        )


# ── Retry Node Executor ────────────────────────────────────────────────────────

class RetryNodeExecutor(BaseNodeExecutor):
    """
    Wraps another node execution with a RetryPolicy.

    Uses the RetryEngine from retry.py.
    Delegates to DispatchingExecutor for the inner node type.
    """

    def __init__(self, inner_executor: "DispatchingExecutor") -> None:
        self._inner = inner_executor
        self._retry_engine = RetryEngine()

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, RetryNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, RetryNode)
        inner_node = node.inner_node
        policy = RetryPolicy(
            max_attempts=node.max_retry_attempts,
            base_delay_ms=node.retry_delay_ms,
            backoff_factor=node.retry_backoff_factor,
        )
        return await self._retry_engine.execute_with_retry(
            fn=lambda: self._inner.execute(inner_node, state),
            node_id=node.node_id,
            workflow_id=state.workflow_id,
            policy=policy,
        )


# ── Loop Node Executor ─────────────────────────────────────────────────────────

class LoopNodeExecutor(BaseNodeExecutor):
    """
    Iterates over a collection from upstream state.
    Executes inner_node once per item, collecting results.
    """

    def __init__(self, inner_executor: "DispatchingExecutor") -> None:
        self._inner = inner_executor

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, LoopNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, LoopNode)
        t_start = time.time()

        # Resolve collection from upstream
        collection_sr = state.step_results.get(node.collection_source_node_id)
        if collection_sr is None:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.FAILED,
                error=f"Loop source node {node.collection_source_node_id!r} not found in results",
            )
        raw_value = collection_sr.value if isinstance(collection_sr, StepResult) else collection_sr
        collection = raw_value if isinstance(raw_value, (list, tuple)) else [raw_value]

        if len(collection) > node.max_iterations:
            collection = list(collection)[: node.max_iterations]

        results: list[Any] = []
        for i, item in enumerate(collection):
            # Inject loop variable into state context via a transient result
            loop_state = WorkflowState(
                workflow_id=state.workflow_id,
                trace_id=state.trace_id,
                task_id=state.task_id,
                step_results={
                    **state.step_results,
                    f"__loop_item_{node.node_id}": StepResult(
                        node_id=f"__loop_item_{node.node_id}",
                        status=StepStatus.COMPLETED,
                        value={"item": item, "index": i, "total": len(collection)},
                    ),
                },
                completed_nodes=set(state.completed_nodes),
                failed_nodes=set(state.failed_nodes),
                skipped_nodes=set(state.skipped_nodes),
            )
            sr = await self._inner.execute(node.inner_node, loop_state)
            results.append(sr.value)
            if sr.status != StepStatus.COMPLETED and node.fail_fast:
                return StepResult(
                    node_id=node.node_id,
                    status=StepStatus.FAILED,
                    error=f"Loop failed at iteration {i}: {sr.error}",
                    value=results,
                    latency_ms=round((time.time() - t_start) * 1000, 1),
                )

        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value={"results": results, "iterations": len(collection)},
            latency_ms=round((time.time() - t_start) * 1000, 1),
        )


# ── Approval Node Executor ─────────────────────────────────────────────────────

class ApprovalNodeExecutor(BaseNodeExecutor):
    """
    Approval gate. In auto-approve mode, passes through.
    In manual mode, stores the pending approval and waits (async, bounded).
    """

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, ApprovalNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, ApprovalNode)
        t_start = time.time()

        if node.auto_approve:
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED,
                value={"approved": True, "approver": "auto", "reason": node.auto_approve_reason},
                latency_ms=0.0,
            )

        # Non-auto: wait for approval via the node's event (set externally)
        if node._approval_event is None:
            import asyncio as _asyncio
            node._approval_event = _asyncio.Event()

        try:
            await asyncio.wait_for(
                node._approval_event.wait(),
                timeout=node.timeout_ms / 1000.0,
            )
            approved = node._approval_result
        except asyncio.TimeoutError:
            approved = False
            node._approval_result = False

        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED if approved else StepStatus.FAILED,
            value={"approved": approved, "approver": node._approver_id},
            error="" if approved else "Approval denied or timed out",
            latency_ms=round((time.time() - t_start) * 1000, 1),
        )


# ── Human Input Node Executor ──────────────────────────────────────────────────

class HumanInputNodeExecutor(BaseNodeExecutor):
    """
    Pauses execution and injects a placeholder for human input.
    In async deployments, the workflow is suspended until input arrives.
    """

    def can_handle(self, node: GraphNode) -> bool:
        return isinstance(node, HumanInputNode)

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        assert isinstance(node, HumanInputNode)
        t_start = time.time()

        if node._input_value is not None:
            # Input was pre-populated (e.g., during replay or test)
            return StepResult(
                node_id=node.node_id,
                status=StepStatus.COMPLETED,
                value={"input": node._input_value, "prompt": node.prompt},
                latency_ms=0.0,
            )

        # No input available — produce a PENDING result
        # The engine should persist checkpoint here and await external wakeup
        log.info(
            "HumanInputNode awaiting input",
            extra={"ctx_node_id": node.node_id, "ctx_prompt": node.prompt[:80]},
        )
        return StepResult(
            node_id=node.node_id,
            status=StepStatus.FAILED,
            error=f"Human input required: {node.prompt}",
            value={"pending_input": True, "prompt": node.prompt, "input_schema": node.input_schema},
            latency_ms=round((time.time() - t_start) * 1000, 1),
        )


# ── Dispatching Executor ───────────────────────────────────────────────────────

class DispatchingExecutor:
    """
    Routes each node to the appropriate executor by type.

    Usage:
        exec = DispatchingExecutor.build(kernel)
        result = await exec.execute(node, workflow_state)

    This is the single executor reference the ExecutionEngine should hold.
    """

    def __init__(self, executors: list[BaseNodeExecutor]) -> None:
        self._executors = executors

    @classmethod
    def build(cls, kernel: "AEOSKernel") -> "DispatchingExecutor":
        """Build a fully configured DispatchingExecutor from a kernel."""
        placeholder = cls([])  # Temporary; RetryNode/LoopNode need circular ref
        executors: list[BaseNodeExecutor] = [
            AgentNodeExecutor(kernel),
            CapabilityNodeExecutor(kernel),
            ToolNodeExecutor(kernel),
            ConditionalNodeExecutor(),
            ParallelNodeExecutor(),
            JoinNodeExecutor(),
            ApprovalNodeExecutor(),
            HumanInputNodeExecutor(),
            RetryNodeExecutor(placeholder),
            LoopNodeExecutor(placeholder),
        ]
        instance = cls(executors)
        # Fix circular references in RetryNode/LoopNode executors
        for ex in executors:
            if isinstance(ex, (RetryNodeExecutor, LoopNodeExecutor)):
                ex._inner = instance
        return instance

    async def execute(self, node: GraphNode, state: WorkflowState) -> StepResult:
        """Dispatch to the first matching executor."""
        for executor in self._executors:
            if executor.can_handle(node):
                return await executor.execute(node, state)

        # Fallback: unknown node type
        log.warning(
            "No executor found for node type — returning COMPLETED placeholder",
            extra={"ctx_node_id": node.node_id, "ctx_node_type": node.node_type},
        )
        return StepResult(
            node_id=node.node_id,
            status=StepStatus.COMPLETED,
            value={"message": f"No executor for node type {node.node_type!r}"},
            latency_ms=0.0,
        )


# Alias
CompositeExecutor = DispatchingExecutor
