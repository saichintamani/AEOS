"""
Wave 9B.3.8 — Global Runtime Knowledge Graph

In-memory graph of KnowledgeNodes and KnowledgeEdges.
Used by the reasoning and decision layers to query causal relationships:
  "Which worker caused failures for model X?"
  "Which tasks depend on agent Y?"
  "What is the failure chain for workflow Z?"

KnowledgeGraph is asyncio-lock protected for safe concurrent access.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Iterable

from app.runtime_intelligence.contracts import (
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
)

logger = logging.getLogger(__name__)


class KnowledgeGraph:
    """
    Directed property graph of runtime entities.

    Indexes maintained:
      - node_id → KnowledgeNode
      - node_type → {node_id}
      - label → {node_id}
      - out-edges: from_node_id → [KnowledgeEdge]
      - in-edges:  to_node_id   → [KnowledgeEdge]
    """

    def __init__(self) -> None:
        self._nodes: dict[str, KnowledgeNode] = {}
        self._edges: dict[str, KnowledgeEdge] = {}
        self._type_index: dict[KnowledgeNodeType, set[str]] = defaultdict(set)
        self._label_index: dict[str, set[str]] = defaultdict(set)
        self._out_edges: dict[str, list[str]] = defaultdict(list)   # node_id → [edge_id]
        self._in_edges: dict[str, list[str]]  = defaultdict(list)   # node_id → [edge_id]
        self._lock = asyncio.Lock()

    # ── Nodes ─────────────────────────────────────────────────────────────────

    async def add_node(self, node: KnowledgeNode) -> None:
        async with self._lock:
            self._nodes[node.node_id] = node
            self._type_index[node.node_type].add(node.node_id)
            if node.label:
                self._label_index[node.label].add(node.node_id)

    async def get_node(self, node_id: str) -> KnowledgeNode | None:
        async with self._lock:
            return self._nodes.get(node_id)

    async def remove_node(self, node_id: str) -> None:
        async with self._lock:
            node = self._nodes.pop(node_id, None)
            if node:
                self._type_index[node.node_type].discard(node_id)
                if node.label:
                    self._label_index[node.label].discard(node_id)
            # Remove incident edges
            for eid in list(self._out_edges.get(node_id, [])):
                self._remove_edge_unsafe(eid)
            for eid in list(self._in_edges.get(node_id, [])):
                self._remove_edge_unsafe(eid)

    async def nodes_by_type(self, node_type: KnowledgeNodeType) -> list[KnowledgeNode]:
        async with self._lock:
            return [self._nodes[nid] for nid in self._type_index.get(node_type, set())
                    if nid in self._nodes]

    async def nodes_by_label(self, label: str) -> list[KnowledgeNode]:
        async with self._lock:
            return [self._nodes[nid] for nid in self._label_index.get(label, set())
                    if nid in self._nodes]

    # ── Edges ─────────────────────────────────────────────────────────────────

    async def add_edge(self, edge: KnowledgeEdge) -> None:
        async with self._lock:
            self._edges[edge.edge_id] = edge
            self._out_edges[edge.from_node_id].append(edge.edge_id)
            self._in_edges[edge.to_node_id].append(edge.edge_id)

    async def get_edge(self, edge_id: str) -> KnowledgeEdge | None:
        async with self._lock:
            return self._edges.get(edge_id)

    async def out_edges(
        self, node_id: str, relation: str | None = None
    ) -> list[KnowledgeEdge]:
        async with self._lock:
            edges = [self._edges[eid] for eid in self._out_edges.get(node_id, [])
                     if eid in self._edges]
            if relation:
                edges = [e for e in edges if e.relation == relation]
            return edges

    async def in_edges(
        self, node_id: str, relation: str | None = None
    ) -> list[KnowledgeEdge]:
        async with self._lock:
            edges = [self._edges[eid] for eid in self._in_edges.get(node_id, [])
                     if eid in self._edges]
            if relation:
                edges = [e for e in edges if e.relation == relation]
            return edges

    async def neighbors(
        self,
        node_id: str,
        relation: str | None = None,
        direction: str = "out",   # "out" | "in" | "both"
    ) -> list[KnowledgeNode]:
        if direction in ("out", "both"):
            edges_out = await self.out_edges(node_id, relation)
        else:
            edges_out = []
        if direction in ("in", "both"):
            edges_in = await self.in_edges(node_id, relation)
        else:
            edges_in = []

        neighbor_ids: set[str] = set()
        for e in edges_out:
            neighbor_ids.add(e.to_node_id)
        for e in edges_in:
            neighbor_ids.add(e.from_node_id)

        async with self._lock:
            return [self._nodes[nid] for nid in neighbor_ids if nid in self._nodes]

    # ── Query ─────────────────────────────────────────────────────────────────

    async def query(
        self,
        node_type: KnowledgeNodeType | None = None,
        relation: str | None = None,
        property_filter: dict | None = None,
    ) -> list[KnowledgeNode]:
        async with self._lock:
            if node_type:
                candidates = [self._nodes[nid]
                              for nid in self._type_index.get(node_type, set())
                              if nid in self._nodes]
            else:
                candidates = list(self._nodes.values())

            if property_filter:
                filtered = []
                for node in candidates:
                    if all(node.properties.get(k) == v for k, v in property_filter.items()):
                        filtered.append(node)
                candidates = filtered

            return candidates

    async def count(self) -> tuple[int, int]:
        """Returns (node_count, edge_count)."""
        async with self._lock:
            return len(self._nodes), len(self._edges)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _remove_edge_unsafe(self, edge_id: str) -> None:
        edge = self._edges.pop(edge_id, None)
        if edge:
            try:
                self._out_edges[edge.from_node_id].remove(edge_id)
            except ValueError:
                pass
            try:
                self._in_edges[edge.to_node_id].remove(edge_id)
            except ValueError:
                pass
