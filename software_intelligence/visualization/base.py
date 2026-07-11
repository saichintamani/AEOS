"""
Software Intelligence Platform — Visualization Interfaces
=========================================================
Abstract base classes and data transfer objects for all visualizations.

Visualization types:
  DependencyGraphViz   — interactive module dependency graph
  ArchitectureViz      — layered architecture diagram
  KnowledgeGraphViz    — force-directed knowledge graph
  MetricsDashboard     — health scores + trend charts
  DebtHeatmap          — technical debt intensity per file
  SecurityHeatmap      — vulnerability density per module

Rendering backends are pluggable. Provided adapters:
  MermaidRenderer      — outputs Mermaid diagram syntax (renders in GitHub/Notion)
  DotRenderer          — outputs Graphviz DOT (renders with dot/neato CLI)
  JsonRenderer         — outputs pure JSON (for front-end charting libs: D3/ECharts)
  AsciiRenderer        — plain-text fallback (for terminal output)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ── Rendering output ───────────────────────────────────────────────────────────

@dataclass
class VizOutput:
    """Result of any rendering pass."""
    format: str               # "mermaid" | "dot" | "json" | "ascii" | "html"
    content: str              # rendered string
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Abstract visualization ──────────────────────────────────────────────────────

class BaseVisualization(ABC):
    """
    A visualization takes structured analysis data and emits VizOutput.
    Subclasses define the data they need; render() drives output format.
    """

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def render(self, format: str = "json") -> VizOutput: ...

    def render_all(self) -> dict[str, VizOutput]:
        outputs = {}
        for fmt in self.supported_formats:
            try:
                outputs[fmt] = self.render(format=fmt)
            except NotImplementedError:
                pass
        return outputs

    @property
    def supported_formats(self) -> list[str]:
        return ["json"]


# ── Dependency graph visualization ─────────────────────────────────────────────

class DependencyGraphViz(BaseVisualization):
    """
    Renders the module dependency graph.

    Formats:
      dot      — Graphviz DOT for graphical rendering
      mermaid  — Mermaid flowchart for GitHub markdown
      json     — D3-compatible node-link JSON
    """

    name = "dependency_graph"
    supported_formats = ["dot", "mermaid", "json"]

    def __init__(self, graph_data: dict) -> None:
        """graph_data: output of DependencyGraphBuilder.export_dot() or .to_dict()"""
        self._data = graph_data

    def render(self, format: str = "json") -> VizOutput:
        if format == "dot":
            return VizOutput(format="dot", content=self._to_dot())
        if format == "mermaid":
            return VizOutput(format="mermaid", content=self._to_mermaid())
        return VizOutput(format="json", content=self._to_json())

    def _to_dot(self) -> str:
        if isinstance(self._data, str):
            return self._data    # already DOT
        import json
        return f"// DOT rendering requires DependencyGraph.export_dot()\n{json.dumps(self._data, indent=2)}"

    def _to_mermaid(self) -> str:
        nodes = self._data.get("nodes", []) if isinstance(self._data, dict) else []
        edges = self._data.get("edges", []) if isinstance(self._data, dict) else []
        lines = ["graph LR"]
        for edge in edges[:50]:   # Mermaid becomes unreadable beyond ~50 edges
            src = edge.get("source", "").replace("/", "_").replace(".", "_")
            tgt = edge.get("target", "").replace("/", "_").replace(".", "_")
            lines.append(f"  {src} --> {tgt}")
        return "\n".join(lines)

    def _to_json(self) -> str:
        import json
        if isinstance(self._data, str):
            return json.dumps({"raw_dot": self._data})
        return json.dumps(self._data, indent=2)


# ── Architecture diagram ───────────────────────────────────────────────────────

class ArchitectureViz(BaseVisualization):
    """
    Renders the reconstructed layered architecture.
    Shows layers (API, Domain, Infrastructure, etc.) and boundaries.
    """

    name = "architecture"
    supported_formats = ["mermaid", "ascii", "json"]

    def __init__(self, architecture_data: dict) -> None:
        self._data = architecture_data

    def render(self, format: str = "mermaid") -> VizOutput:
        if format == "mermaid":
            return VizOutput(format="mermaid", content=self._to_mermaid())
        if format == "ascii":
            return VizOutput(format="ascii", content=self._to_ascii())
        import json
        return VizOutput(format="json", content=json.dumps(self._data, indent=2))

    def _to_mermaid(self) -> str:
        components = self._data.get("components", [])
        # Group by layer
        layers: dict[str, list[str]] = {}
        for c in components:
            layer = c.get("layer", "unknown")
            layers.setdefault(layer, []).append(c.get("name", ""))
        lines = ["graph TB"]
        for layer, names in layers.items():
            subgraph_id = layer.replace(" ", "_")
            lines.append(f"  subgraph {subgraph_id}[{layer.title()}]")
            for name in names[:5]:
                lines.append(f"    {name.replace('.', '_').replace('/', '_')}")
            lines.append("  end")
        return "\n".join(lines)

    def _to_ascii(self) -> str:
        layers = self._data.get("layers", [])
        if not layers:
            return "No layers detected."
        width = 60
        lines = ["=" * width, " ARCHITECTURE LAYERS ".center(width), "=" * width]
        for layer in layers:
            lines.append(f"  ┌{'─' * (width - 4)}┐")
            lines.append(f"  │ {layer.upper():<{width-6}} │")
            components = self._data.get("by_layer", {}).get(layer, [])
            for c in components[:3]:
                lines.append(f"  │   • {c:<{width-9}} │")
            lines.append(f"  └{'─' * (width - 4)}┘")
        lines.append("=" * width)
        return "\n".join(lines)


# ── Knowledge graph visualization ──────────────────────────────────────────────

class KnowledgeGraphViz(BaseVisualization):
    """
    Force-directed visualization of the knowledge graph.
    JSON output is compatible with D3 force-directed layout / Cytoscape.js.
    """

    name = "knowledge_graph"
    supported_formats = ["json", "dot"]

    def __init__(self, graph_dict: dict) -> None:
        """graph_dict: output of KnowledgeGraph.to_dict()"""
        self._data = graph_dict

    def render(self, format: str = "json") -> VizOutput:
        import json
        if format == "dot":
            return VizOutput(format="dot", content=self._to_dot())
        # D3 force-directed format
        d3_data = {
            "nodes": [
                {
                    "id":    n["node_id"],
                    "label": n["label"],
                    "group": n["kind"],
                }
                for n in self._data.get("nodes", [])
            ],
            "links": [
                {
                    "source": e["source_id"],
                    "target": e["target_id"],
                    "type":   e["kind"],
                    "value":  e.get("weight", 1.0),
                }
                for e in self._data.get("edges", [])
            ],
        }
        return VizOutput(format="json", content=json.dumps(d3_data, indent=2))

    def _to_dot(self) -> str:
        lines = ['digraph knowledge_graph {', '  rankdir=LR;']
        for node in self._data.get("nodes", []):
            label = node["label"].replace('"', '\\"')
            lines.append(f'  "{node["node_id"]}" [label="{label}"];')
        for edge in self._data.get("edges", [])[:200]:
            lines.append(f'  "{edge["source_id"]}" -> "{edge["target_id"]}" [label="{edge["kind"]}"];')
        lines.append("}")
        return "\n".join(lines)


# ── Metrics dashboard ──────────────────────────────────────────────────────────

class MetricsDashboard(BaseVisualization):
    """
    Health score dashboard. JSON output drives front-end gauge/radar charts.
    ASCII output provides a quick terminal overview.
    """

    name = "metrics_dashboard"
    supported_formats = ["json", "ascii"]

    def __init__(self, health_report: Any) -> None:
        self._report = health_report

    def render(self, format: str = "ascii") -> VizOutput:
        if format == "ascii":
            return VizOutput(format="ascii", content=self._to_ascii())
        import json
        scores = self._report.scores
        return VizOutput(format="json", content=json.dumps({
            "repo_id": self._report.repo_id,
            "overall": scores.overall,
            "grade":   scores.overall_grade,
            "dimensions": {
                "security":       scores.security,
                "maintainability": scores.maintainability,
                "test_coverage":  scores.test_coverage,
                "documentation":  scores.documentation,
                "complexity":     scores.complexity,
            },
            "top_risks": [
                {"title": r.title, "severity": r.severity} for r in self._report.top_risks[:5]
            ],
        }, indent=2))

    def _to_ascii(self) -> str:
        s = self._report.scores
        bar = lambda v: ("█" * int(v // 10)).ljust(10)
        lines = [
            f"{'═' * 50}",
            f" HEALTH DASHBOARD: {self._report.repo_id}",
            f"{'═' * 50}",
            f" Overall:         {bar(s.overall)} {s.overall:5.1f}  {s.overall_grade}",
            f" Security:        {bar(s.security)} {s.security:5.1f}",
            f" Maintainability: {bar(s.maintainability)} {s.maintainability:5.1f}",
            f" Test Coverage:   {bar(s.test_coverage)} {s.test_coverage:5.1f}",
            f" Documentation:   {bar(s.documentation)} {s.documentation:5.1f}",
            f" Complexity:      {bar(s.complexity)} {s.complexity:5.1f}",
            f"{'═' * 50}",
        ]
        return "\n".join(lines)


# ── Debt heatmap ───────────────────────────────────────────────────────────────

class DebtHeatmap(BaseVisualization):
    """Per-file debt intensity heatmap (JSON for front-end treemap rendering)."""

    name = "debt_heatmap"
    supported_formats = ["json", "ascii"]

    def __init__(self, debt_report: Any) -> None:
        self._report = debt_report

    def render(self, format: str = "json") -> VizOutput:
        import json
        # Aggregate debt hours per file
        file_debt: dict[str, float] = {}
        for item in self._report.items:
            file_debt[item.file_path] = file_debt.get(item.file_path, 0.0) + item.effort_hours

        if format == "ascii":
            lines = [" DEBT HEATMAP (top 10 files)", "-" * 50]
            sorted_files = sorted(file_debt.items(), key=lambda x: x[1], reverse=True)[:10]
            for fp, hrs in sorted_files:
                bar = "█" * min(int(hrs), 30)
                lines.append(f" {fp[-35:]:35s} {bar} {hrs:.1f}h")
            return VizOutput(format="ascii", content="\n".join(lines))

        treemap_data = [
            {"name": fp, "value": hrs, "severity": "high" if hrs > 8 else "medium"}
            for fp, hrs in sorted(file_debt.items(), key=lambda x: x[1], reverse=True)
        ]
        return VizOutput(format="json", content=json.dumps({
            "type": "treemap",
            "data": treemap_data,
            "total_hours": self._report.total_debt_hours,
        }, indent=2))
