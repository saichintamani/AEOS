"""
AEOS Distributed Execution Engine — Graph Visualization

Exports ExecutionGraph to multiple formats:
  - Mermaid flowchart (for GitHub README, Notion, documentation)
  - Graphviz DOT (for graphviz / CI rendering)
  - JSON (for custom frontends or API responses)
  - ASCII art (for terminal output)

When a WorkflowState is provided, nodes are colored by their execution status:
  ✅ completed (green)  ❌ failed (red)  ⏭️ skipped (gray)  ⏳ pending (blue)
"""

from __future__ import annotations

import json
from typing import Any

from app.execution.graph import (
    AgentNode,
    ConditionalNode,
    GraphEdge,
    GraphNode,
    JoinNode,
    ParallelNode,
    SequentialEdge,
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
from app.execution.schemas import ExecutionGraph, StepStatus, WorkflowState

__all__ = ["GraphVisualizer"]


# ── Helpers ───────────────────────────────────────────────────────────────────

_NODE_EMOJI = {
    "agent":      "🤖",
    "capability": "⚡",
    "tool":       "🔧",
    "conditional":"❓",
    "parallel":   "⊕",
    "join":       "⊗",
    "merge":      "⊗",
    "retry":      "🔄",
    "loop":       "🔁",
    "approval":   "✅",
    "human_input":"👤",
    "base":       "●",
}

_STATUS_MERMAID_STYLE = {
    StepStatus.COMPLETED: "fill:#22c55e,color:#fff",  # green
    StepStatus.FAILED:    "fill:#ef4444,color:#fff",  # red
    StepStatus.TIMED_OUT: "fill:#f97316,color:#fff",  # orange
    StepStatus.SKIPPED:   "fill:#94a3b8,color:#fff",  # slate
}

_STATUS_DOT_STYLE = {
    StepStatus.COMPLETED: 'style=filled fillcolor="#22c55e" fontcolor="#ffffff"',
    StepStatus.FAILED:    'style=filled fillcolor="#ef4444" fontcolor="#ffffff"',
    StepStatus.TIMED_OUT: 'style=filled fillcolor="#f97316" fontcolor="#ffffff"',
    StepStatus.SKIPPED:   'style=filled fillcolor="#94a3b8" fontcolor="#ffffff"',
}


def _node_label(node: GraphNode, short: bool = False) -> str:
    emoji = _NODE_EMOJI.get(node.node_type, "●")
    if isinstance(node, AgentNode):
        desc = node.task_description[:40] if short else node.task_description[:80]
        return f"{emoji} {node.agent_type}\n{desc}"
    if isinstance(node, CapabilityNode):
        return f"{emoji} cap:{node.capability}\n{node.task_description[:40]}"
    if isinstance(node, ToolNode):
        return f"{emoji} tool:{node.tool_id}"
    if isinstance(node, ConditionalNode):
        expr = node.condition_expr[:50]
        return f"{emoji} if {expr}"
    if isinstance(node, (JoinNode, MergeNode)):
        strategy = getattr(node, "join_strategy", "all")
        return f"{emoji} merge({strategy})"
    if isinstance(node, RetryNode):
        inner_type = getattr(node.inner_node, "node_type", "?")
        return f"{emoji} retry[{inner_type}] ×{node.max_retry_attempts}"
    if isinstance(node, LoopNode):
        return f"{emoji} loop over {node.collection_source_node_id[:20]}"
    if isinstance(node, ApprovalNode):
        return f"{emoji} approval\n{node.approval_prompt[:40]}"
    if isinstance(node, HumanInputNode):
        return f"{emoji} input\n{node.prompt[:40]}"
    return f"{emoji} {node.node_type}:{node.node_id[:8]}"


def _mermaid_safe(s: str) -> str:
    """Escape string for Mermaid label."""
    return s.replace('"', "'").replace("\n", "<br/>").replace("[", "(").replace("]", ")")


def _dot_escape(s: str) -> str:
    """Escape string for DOT label."""
    return s.replace('"', '\\"').replace("\n", "\\n")


def _node_status(node_id: str, state: WorkflowState | None) -> StepStatus | None:
    if state is None:
        return None
    if node_id in state.completed_nodes:
        return StepStatus.COMPLETED
    if node_id in state.failed_nodes:
        return StepStatus.FAILED
    if node_id in state.skipped_nodes:
        return StepStatus.SKIPPED
    return None


class GraphVisualizer:
    """
    Renders ExecutionGraph to various output formats.

    All methods are pure functions — no side effects.
    """

    # ── Mermaid ───────────────────────────────────────────────────────────────

    def to_mermaid(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
        direction: str = "TD",
    ) -> str:
        """
        Render as a Mermaid flowchart.

        Args:
            graph:          The execution graph
            workflow_state: Optional; if provided, nodes are colored by status
            direction:      "TD" (top-down) or "LR" (left-right)

        Returns:
            Mermaid markdown string — paste into GitHub or a Mermaid renderer.
        """
        lines: list[str] = [f"flowchart {direction}"]

        node_map = {n.node_id: n for n in graph.nodes}
        styled_nodes: set[str] = set()

        # Node definitions
        for node in graph.nodes:
            nid = node.node_id[:12].replace("-", "")
            label = _mermaid_safe(_node_label(node))
            status = _node_status(node.node_id, workflow_state)

            # Node shape by type
            if isinstance(node, ConditionalNode):
                lines.append(f'    {nid}{{{"{label}"}}}')
            elif isinstance(node, (JoinNode, MergeNode)):
                lines.append(f'    {nid}(("{label}"))')
            elif isinstance(node, ApprovalNode):
                lines.append(f'    {nid}[/"{label}"\\]')
            elif isinstance(node, HumanInputNode):
                lines.append(f'    {nid}[\\"{label}"/]')
            else:
                lines.append(f'    {nid}["{label}"]')

            if status is not None and status in _STATUS_MERMAID_STYLE:
                styled_nodes.add(f'    style {nid} {_STATUS_MERMAID_STYLE[status]}')

        # Edges
        for edge in graph.edges:
            from_id = edge.from_node_id[:12].replace("-", "")
            to_id = edge.to_node_id[:12].replace("-", "")
            if edge.edge_type == "sequential":
                lines.append(f"    {from_id} --> {to_id}")
            elif edge.edge_type == "conditional":
                cond_val = getattr(edge, "condition_value", True)
                label = "true" if cond_val else "false"
                lines.append(f"    {from_id} -->|{label}| {to_id}")
            elif edge.edge_type == "data_flow":
                keys = getattr(edge, "data_keys", [])
                label = ",".join(keys[:3]) if keys else "data"
                lines.append(f"    {from_id} -.->|{label}| {to_id}")
            else:
                lines.append(f"    {from_id} --> {to_id}")

        # Style overrides
        lines.extend(sorted(styled_nodes))

        return "\n".join(lines)

    # ── Graphviz DOT ──────────────────────────────────────────────────────────

    def to_dot(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
        graph_name: str = "AEOS_Execution_Graph",
    ) -> str:
        """
        Render as Graphviz DOT format.

        Pipe to `dot -Tsvg -o output.svg` to render.
        """
        lines: list[str] = [
            f'digraph "{graph_name}" {{',
            "    rankdir=TD;",
            "    node [shape=box fontname=Helvetica fontsize=11];",
            "    edge [fontname=Helvetica fontsize=9];",
            "",
        ]

        for node in graph.nodes:
            nid = f'n{node.node_id[:12].replace("-", "")}'
            label = _dot_escape(_node_label(node, short=True))
            status = _node_status(node.node_id, workflow_state)

            # Shape by type
            if isinstance(node, ConditionalNode):
                shape = "diamond"
            elif isinstance(node, (JoinNode, MergeNode)):
                shape = "ellipse"
            elif isinstance(node, ApprovalNode):
                shape = "parallelogram"
            elif isinstance(node, HumanInputNode):
                shape = "trapezium"
            else:
                shape = "box"

            style_str = _STATUS_DOT_STYLE.get(status, "")
            if style_str:
                lines.append(f'    {nid} [label="{label}" shape={shape} {style_str}];')
            else:
                lines.append(f'    {nid} [label="{label}" shape={shape}];')

        lines.append("")

        for edge in graph.edges:
            from_id = f'n{edge.from_node_id[:12].replace("-", "")}'
            to_id = f'n{edge.to_node_id[:12].replace("-", "")}'
            if edge.edge_type == "conditional":
                cond = "true" if getattr(edge, "condition_value", True) else "false"
                lines.append(f'    {from_id} -> {to_id} [label="{cond}" style=dashed];')
            elif edge.edge_type == "data_flow":
                keys = getattr(edge, "data_keys", [])
                lbl = ",".join(keys[:2]) if keys else "data"
                lines.append(f'    {from_id} -> {to_id} [label="{lbl}" style=dotted];')
            else:
                lines.append(f"    {from_id} -> {to_id};")

        lines += ["", "}"]
        return "\n".join(lines)

    # ── JSON ──────────────────────────────────────────────────────────────────

    def to_json(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
        indent: int = 2,
    ) -> dict[str, Any]:
        """
        Render as a structured JSON dict (suitable for API responses).
        """
        nodes_out: list[dict] = []
        for node in graph.nodes:
            status = _node_status(node.node_id, workflow_state)
            step_result = None
            if workflow_state and node.node_id in workflow_state.step_results:
                sr = workflow_state.step_results[node.node_id]
                if hasattr(sr, "status"):
                    step_result = {
                        "status": sr.status.value,
                        "latency_ms": sr.latency_ms,
                        "confidence": sr.confidence,
                        "error": sr.error,
                    }
            node_dict: dict[str, Any] = {
                "node_id": node.node_id,
                "node_type": node.node_type,
                "label": _node_label(node, short=True),
                "priority": node.priority,
                "timeout_ms": node.timeout_ms,
                "goal_id": node.goal_id,
                "execution_status": status.value if status else "pending",
            }
            if step_result:
                node_dict["step_result"] = step_result
            # Type-specific fields
            if isinstance(node, AgentNode):
                node_dict["agent_type"] = node.agent_type
                node_dict["task_description"] = node.task_description
            elif isinstance(node, CapabilityNode):
                node_dict["capability"] = node.capability
            nodes_out.append(node_dict)

        edges_out: list[dict] = [
            {
                "edge_id": e.edge_id,
                "from_node_id": e.from_node_id,
                "to_node_id": e.to_node_id,
                "edge_type": e.edge_type,
            }
            for e in graph.edges
        ]

        result: dict[str, Any] = {
            "graph_id": graph.graph_id,
            "compiled_at": graph.compiled_at,
            "total_timeout_ms": graph.total_timeout_ms,
            "topological_order": graph.topological_order,
            "parallel_groups": graph.parallel_groups,
            "nodes": nodes_out,
            "edges": edges_out,
        }

        if workflow_state:
            result["workflow"] = {
                "workflow_id": workflow_state.workflow_id,
                "status": workflow_state.status.value,
                "completed_nodes": list(workflow_state.completed_nodes),
                "failed_nodes": list(workflow_state.failed_nodes),
                "skipped_nodes": list(workflow_state.skipped_nodes),
                "revision_count": workflow_state.revision_count,
            }

        return result

    def to_json_string(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
        indent: int = 2,
    ) -> str:
        """Return JSON as a formatted string."""
        return json.dumps(self.to_json(graph, workflow_state), indent=indent, default=str)

    # ── ASCII art ─────────────────────────────────────────────────────────────

    def to_ascii(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
    ) -> str:
        """
        Simple ASCII representation — useful for terminal/log output.
        """
        lines: list[str] = [
            f"Execution Graph: {graph.graph_id[:8]}",
            f"Nodes: {len(graph.nodes)}  Edges: {len(graph.edges)}",
            "─" * 60,
        ]

        node_map = {n.node_id: n for n in graph.nodes}

        for i, node_id in enumerate(graph.topological_order):
            node = node_map.get(node_id)
            if node is None:
                continue
            status = _node_status(node_id, workflow_state)
            status_icon = {
                StepStatus.COMPLETED: "✓",
                StepStatus.FAILED:    "✗",
                StepStatus.TIMED_OUT: "T",
                StepStatus.SKIPPED:   "~",
            }.get(status, "·")

            emoji = _NODE_EMOJI.get(node.node_type, "●")
            label = _node_label(node, short=True).replace("\n", " | ")
            prefix = f"  [{i:02d}] [{status_icon}] {emoji} "
            lines.append(f"{prefix}{label}")

        lines.append("─" * 60)

        if workflow_state:
            c = len(workflow_state.completed_nodes)
            f = len(workflow_state.failed_nodes)
            s = len(workflow_state.skipped_nodes)
            lines.append(
                f"  Completed: {c}  Failed: {f}  Skipped: {s}  "
                f"Status: {workflow_state.status.value}"
            )

        return "\n".join(lines)

    # ── HTML (Mermaid embedded) ────────────────────────────────────────────────

    def to_html(
        self,
        graph: ExecutionGraph,
        workflow_state: WorkflowState | None = None,
        title: str = "AEOS Execution Graph",
    ) -> str:
        """
        Embed the Mermaid diagram in a self-contained HTML page.
        """
        mermaid_code = self.to_mermaid(graph, workflow_state)
        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title}</title>
  <script src="https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js"></script>
</head>
<body style="background:#0f172a;color:#e2e8f0;font-family:monospace;padding:2rem;">
  <h2>{title}</h2>
  <div class="mermaid">
{mermaid_code}
  </div>
  <script>mermaid.initialize({{startOnLoad:true,theme:'dark'}});</script>
</body>
</html>"""
