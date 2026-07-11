"""
AEOS Distributed Execution Engine — Extended Node Type Catalog

Extends the base node types from graph.py with:
  - CapabilityNode  — dispatch by kernel capability (not agent type)
  - RetryNode       — wraps any inner node with a RetryPolicy
  - LoopNode        — iterate a node over a collection
  - ApprovalNode    — human or automated approval gate
  - HumanInputNode  — pause for interactive human input
  - MergeNode       — multi-strategy result merger (richer than JoinNode)

Also provides a NodeFactory for programmatic graph construction.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.execution.graph import GraphNode, AgentNode, ToolNode, JoinNode

__all__ = [
    "CapabilityNode",
    "RetryNode",
    "LoopNode",
    "ApprovalNode",
    "HumanInputNode",
    "MergeNode",
    "NodeFactory",
]


def _uid() -> str:
    return str(uuid.uuid4())


# ── CapabilityNode ────────────────────────────────────────────────────────────

@dataclass
class CapabilityNode(GraphNode):
    """
    Dispatches by kernel capability rather than agent type.

    The kernel's service registry is queried for services that expose
    the given capability. The first matching service is used.

    Example:
        CapabilityNode(capability="code_analysis", task_description="Review this PR")
    """
    node_type: str = "capability"
    capability: str = ""                  # e.g., "code_analysis", "web_search"
    task_description: str = ""
    output_key: str = ""
    prefer_service_id: str = ""           # Prefer a specific service if available


# ── RetryNode ─────────────────────────────────────────────────────────────────

@dataclass
class RetryNode(GraphNode):
    """
    Wraps any inner node with configurable retry behavior.

    The inner node is executed up to max_retry_attempts times.
    Between retries, a delay is applied with exponential backoff + jitter.

    Example:
        RetryNode(
            inner_node=AgentNode(agent_type="research_agent", ...),
            max_retry_attempts=3,
            retry_delay_ms=1000.0,
        )
    """
    node_type: str = "retry"
    inner_node: GraphNode = field(default_factory=lambda: GraphNode())
    max_retry_attempts: int = 3
    retry_delay_ms: float = 1000.0
    retry_backoff_factor: float = 2.0
    retry_on_timeout: bool = True
    retry_on_failure: bool = True

    def __post_init__(self) -> None:
        # Inherit timeout from inner node if not overridden
        if self.timeout_ms == 30_000.0 and self.inner_node.timeout_ms != 30_000.0:
            self.timeout_ms = self.inner_node.timeout_ms * self.max_retry_attempts


# ── LoopNode ──────────────────────────────────────────────────────────────────

@dataclass
class LoopNode(GraphNode):
    """
    Iterates inner_node over each item in a collection from a previous node.

    The collection is read from step_results[collection_source_node_id].value.
    If the value is a list, each element is processed; otherwise treated as
    a single-item collection.

    Example:
        LoopNode(
            collection_source_node_id="research_node",
            inner_node=AgentNode(agent_type="analyst_agent", ...),
            max_iterations=10,
        )
    """
    node_type: str = "loop"
    inner_node: GraphNode = field(default_factory=lambda: GraphNode())
    collection_source_node_id: str = ""
    max_iterations: int = 100
    fail_fast: bool = False               # Stop on first inner failure
    output_key: str = ""


# ── ApprovalNode ──────────────────────────────────────────────────────────────

@dataclass
class ApprovalNode(GraphNode):
    """
    Approval gate that must be satisfied before downstream nodes execute.

    In auto_approve mode, the node passes immediately (useful for testing).
    In manual mode, execution pauses until set_approval() is called.

    The approval state is stored in _approval_event and _approval_result
    (not serialized — these are in-process signaling mechanisms).
    """
    node_type: str = "approval"
    approval_prompt: str = ""             # Message shown to approver
    approver_role: str = "any"            # Role required to approve
    auto_approve: bool = False
    auto_approve_reason: str = "auto-approved"
    require_reason: bool = False          # Require approver to provide reason

    # In-process signaling (not serialized)
    _approval_event: asyncio.Event | None = field(default=None, repr=False, compare=False)
    _approval_result: bool = field(default=False, repr=False, compare=False)
    _approver_id: str = field(default="", repr=False, compare=False)
    _approval_reason: str = field(default="", repr=False, compare=False)

    def set_approval(self, approved: bool, approver_id: str = "", reason: str = "") -> None:
        """
        Externally signal the approval decision.

        Call this from an API endpoint or webhook handler.
        """
        self._approval_result = approved
        self._approver_id = approver_id
        self._approval_reason = reason
        if self._approval_event is not None:
            self._approval_event.set()


# ── HumanInputNode ────────────────────────────────────────────────────────────

@dataclass
class HumanInputNode(GraphNode):
    """
    Pauses execution and waits for human-provided input.

    In a web deployment, the workflow is checkpointed at this point
    and resumed when the input arrives via an API callback.

    For testing and replay, _input_value can be pre-set.
    """
    node_type: str = "human_input"
    prompt: str = ""                      # Question shown to the user
    input_schema: dict[str, Any] = field(default_factory=dict)  # JSON Schema for validation
    default_value: Any = None             # Used if timeout occurs
    output_key: str = ""

    # Pre-populated input (set externally or during replay)
    _input_value: Any = field(default=None, repr=False, compare=False)

    def provide_input(self, value: Any) -> None:
        """Externally provide the human input value."""
        self._input_value = value


# ── MergeNode ─────────────────────────────────────────────────────────────────

@dataclass
class MergeNode(GraphNode):
    """
    Advanced multi-input result merger.

    Extends JoinNode with additional merge strategies:
      - "all"            — require all inputs; merge into dict
      - "first"          — use first successful result
      - "best_quality"   — use highest-confidence result
      - "majority"       — use most common result value
      - "concatenate"    — concatenate all string results
      - "union"          — union of all list/set results
      - "weighted"       — weighted average of numeric results

    fallback_to_partial: If True, succeed with partial results even if some
    inputs failed. If False (default), all inputs must complete.
    """
    node_type: str = "merge"
    input_node_ids: list[str] = field(default_factory=list)
    join_strategy: str = "all"
    output_key: str = ""
    fallback_to_partial: bool = False
    weights: dict[str, float] = field(default_factory=dict)  # node_id → weight (for "weighted")

    def requires_all_inputs(self) -> bool:
        return self.join_strategy == "all" and not self.fallback_to_partial


# ── Node Factory ──────────────────────────────────────────────────────────────

class NodeFactory:
    """
    Fluent builder for common execution graph patterns.

    Usage:
        factory = NodeFactory()
        research = factory.agent("research_agent", "Research quantum computing")
        analyst  = factory.agent("analyst_agent", "Analyze findings")
        retry_r  = factory.with_retry(research, max_attempts=3)
    """

    def agent(
        self,
        agent_type: str,
        task: str,
        priority: int = 5,
        timeout_ms: float = 30_000.0,
        output_key: str = "",
    ) -> AgentNode:
        return AgentNode(
            agent_type=agent_type,
            task_description=task,
            priority=priority,
            timeout_ms=timeout_ms,
            output_key=output_key,
        )

    def capability(
        self,
        capability: str,
        task: str,
        priority: int = 5,
        timeout_ms: float = 30_000.0,
    ) -> CapabilityNode:
        return CapabilityNode(
            capability=capability,
            task_description=task,
            priority=priority,
            timeout_ms=timeout_ms,
        )

    def tool(
        self,
        tool_id: str,
        params: dict[str, Any] | None = None,
        priority: int = 5,
    ) -> ToolNode:
        return ToolNode(
            tool_id=tool_id,
            tool_params=params or {},
            priority=priority,
        )

    def with_retry(
        self,
        inner: GraphNode,
        max_attempts: int = 3,
        delay_ms: float = 1000.0,
        backoff: float = 2.0,
    ) -> RetryNode:
        return RetryNode(
            inner_node=inner,
            max_retry_attempts=max_attempts,
            retry_delay_ms=delay_ms,
            retry_backoff_factor=backoff,
            priority=inner.priority,
            goal_id=inner.goal_id,
        )

    def loop_over(
        self,
        inner: GraphNode,
        source_node_id: str,
        max_iterations: int = 100,
        fail_fast: bool = False,
    ) -> LoopNode:
        return LoopNode(
            inner_node=inner,
            collection_source_node_id=source_node_id,
            max_iterations=max_iterations,
            fail_fast=fail_fast,
        )

    def approval_gate(
        self,
        prompt: str,
        auto_approve: bool = False,
        role: str = "any",
    ) -> ApprovalNode:
        return ApprovalNode(
            approval_prompt=prompt,
            approver_role=role,
            auto_approve=auto_approve,
        )

    def human_input(
        self,
        prompt: str,
        schema: dict[str, Any] | None = None,
        default: Any = None,
    ) -> HumanInputNode:
        return HumanInputNode(
            prompt=prompt,
            input_schema=schema or {},
            default_value=default,
        )

    def merge(
        self,
        input_node_ids: list[str],
        strategy: str = "all",
        partial_ok: bool = False,
    ) -> MergeNode:
        return MergeNode(
            input_node_ids=input_node_ids,
            join_strategy=strategy,
            fallback_to_partial=partial_ok,
        )
