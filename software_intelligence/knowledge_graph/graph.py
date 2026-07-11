"""
Software Intelligence Platform — Knowledge Graph Domain Model
=============================================================
Node and edge schemas for the software knowledge graph.

Node types:
  MODULE    — a source file / compilation unit
  FUNCTION  — a named callable
  CLASS     — a class / struct / interface
  ISSUE     — a GitHub / GitLab issue
  PR        — a pull request
  CONCEPT   — an extracted natural-language concept (from docs/comments)
  AUTHOR    — a commit author
  DEPENDENCY — an external package

Edge types:
  IMPORTS        — module → module
  CALLS          — function → function
  INHERITS       — class → class
  DEFINED_IN     — function/class → module
  REFERENCES     — issue/PR → module/function/class
  FIXES          — PR → issue
  AUTHORED_BY    — commit → author
  DEPENDS_ON     — module → external package
  SIMILAR_TO     — issue → issue (duplicate/related)
  MENTIONS       — issue/PR → concept
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from software_intelligence.schemas import GraphEdgeKind, GraphNode, GraphNodeKind, GraphEdge


@dataclass
class KnowledgeGraph:
    """
    In-memory adjacency representation of the software knowledge graph.
    Nodes are keyed by unique node_id; edges are stored in a flat list
    and in per-node adjacency sets for O(1) neighbour lookup.
    """

    repo_id: str = ""
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge]      = field(default_factory=list)

    # Internal adjacency: node_id → set of (edge_kind, target_id)
    _out: dict[str, set[tuple[str, str]]] = field(default_factory=dict, repr=False)
    _in:  dict[str, set[tuple[str, str]]] = field(default_factory=dict, repr=False)

    # ── Mutation ───────────────────────────────────────────────────────────────

    def add_node(self, node: GraphNode) -> None:
        self.nodes[node.node_id] = node
        self._out.setdefault(node.node_id, set())
        self._in.setdefault(node.node_id, set())

    def add_edge(self, edge: GraphEdge) -> None:
        self.edges.append(edge)
        self._out.setdefault(edge.source_id, set()).add((edge.kind.value, edge.target_id))
        self._in.setdefault(edge.target_id,  set()).add((edge.kind.value, edge.source_id))

    def get_node(self, node_id: str) -> GraphNode | None:
        return self.nodes.get(node_id)

    # ── Traversal ──────────────────────────────────────────────────────────────

    def neighbours(self, node_id: str, kind: str | None = None) -> list[GraphNode]:
        """Outgoing neighbours; optionally filter by edge kind."""
        pairs = self._out.get(node_id, set())
        if kind:
            pairs = {(k, t) for k, t in pairs if k == kind}
        return [self.nodes[t] for _, t in pairs if t in self.nodes]

    def predecessors(self, node_id: str, kind: str | None = None) -> list[GraphNode]:
        """Incoming predecessors; optionally filter by edge kind."""
        pairs = self._in.get(node_id, set())
        if kind:
            pairs = {(k, s) for k, s in pairs if k == kind}
        return [self.nodes[s] for _, s in pairs if s in self.nodes]

    def subgraph(self, node_ids: set[str]) -> "KnowledgeGraph":
        """Return a new KnowledgeGraph restricted to given node IDs."""
        g = KnowledgeGraph(repo_id=self.repo_id)
        for nid in node_ids:
            if nid in self.nodes:
                g.add_node(self.nodes[nid])
        for edge in self.edges:
            if edge.source_id in node_ids and edge.target_id in node_ids:
                g.add_edge(edge)
        return g

    # ── Statistics ─────────────────────────────────────────────────────────────

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)

    def nodes_by_kind(self, kind: GraphNodeKind) -> list[GraphNode]:
        return [n for n in self.nodes.values() if n.kind == kind]

    def edges_by_kind(self, kind: GraphEdgeKind) -> list[GraphEdge]:
        return [e for e in self.edges if e.kind == kind]

    def hub_nodes(self, top_n: int = 10) -> list[tuple[GraphNode, int]]:
        """Return top N nodes by total degree (in + out)."""
        degree = {
            nid: len(self._out.get(nid, set())) + len(self._in.get(nid, set()))
            for nid in self.nodes
        }
        sorted_ids = sorted(degree, key=lambda k: degree[k], reverse=True)[:top_n]
        return [(self.nodes[nid], degree[nid]) for nid in sorted_ids]

    # ── Serialization ──────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "repo_id":   self.repo_id,
            "nodes":     [
                {
                    "node_id": n.node_id,
                    "kind":    n.kind.value,
                    "label":   n.label,
                    "file_path": n.file_path,
                    "metadata": n.metadata,
                }
                for n in self.nodes.values()
            ],
            "edges": [
                {
                    "source_id": e.source_id,
                    "target_id": e.target_id,
                    "kind":      e.kind.value,
                    "weight":    e.weight,
                    "metadata":  e.metadata,
                }
                for e in self.edges
            ],
        }

    def to_dot(self) -> str:
        """Export as Graphviz DOT string."""
        lines = [f'digraph "{self.repo_id}" {{', "  rankdir=LR;"]
        _COLOURS = {
            GraphNodeKind.MODULE:   "#4e79a7",
            GraphNodeKind.FUNCTION: "#f28e2b",
            GraphNodeKind.CLASS:    "#59a14f",
            GraphNodeKind.ISSUE:    "#e15759",
            GraphNodeKind.PR:       "#76b7b2",
            GraphNodeKind.CONCEPT:  "#edc948",
            GraphNodeKind.AUTHOR:   "#b07aa1",
        }
        for node in self.nodes.values():
            colour = _COLOURS.get(node.kind, "#aaaaaa")
            label  = node.label.replace('"', '\\"')
            lines.append(
                f'  "{node.node_id}" [label="{label}" style=filled fillcolor="{colour}"];'
            )
        for edge in self.edges:
            lines.append(
                f'  "{edge.source_id}" -> "{edge.target_id}" [label="{edge.kind.value}"];'
            )
        lines.append("}")
        return "\n".join(lines)
