"""Unit tests — KnowledgeGraph."""

from __future__ import annotations

import pytest

from app.runtime_intelligence.contracts import (
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
)
from app.runtime_intelligence.knowledge_graph import KnowledgeGraph


def _node(node_id: str, node_type: KnowledgeNodeType = KnowledgeNodeType.WORKER,
          label: str = "", **props) -> KnowledgeNode:
    return KnowledgeNode(node_id=node_id, node_type=node_type, label=label, properties=props)


def _edge(edge_id: str, from_id: str, to_id: str, relation: str = "uses") -> KnowledgeEdge:
    return KnowledgeEdge(edge_id=edge_id, from_node_id=from_id, to_node_id=to_id, relation=relation)


class TestKnowledgeGraph:

    @pytest.mark.asyncio
    async def test_add_and_get_node(self):
        g = KnowledgeGraph()
        node = _node("n1", KnowledgeNodeType.WORKER, label="worker-1")
        await g.add_node(node)
        result = await g.get_node("n1")
        assert result is not None
        assert result.label == "worker-1"

    @pytest.mark.asyncio
    async def test_remove_node(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1"))
        await g.remove_node("n1")
        assert await g.get_node("n1") is None

    @pytest.mark.asyncio
    async def test_remove_node_cleans_edges(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1"))
        await g.add_node(_node("n2"))
        await g.add_edge(_edge("e1", "n1", "n2"))
        await g.remove_node("n1")
        assert await g.get_edge("e1") is None

    @pytest.mark.asyncio
    async def test_nodes_by_type(self):
        g = KnowledgeGraph()
        await g.add_node(_node("w1", KnowledgeNodeType.WORKER))
        await g.add_node(_node("m1", KnowledgeNodeType.MODEL))
        await g.add_node(_node("w2", KnowledgeNodeType.WORKER))
        workers = await g.nodes_by_type(KnowledgeNodeType.WORKER)
        assert {n.node_id for n in workers} == {"w1", "w2"}

    @pytest.mark.asyncio
    async def test_nodes_by_label(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1", label="gpu-worker"))
        await g.add_node(_node("n2", label="cpu-worker"))
        results = await g.nodes_by_label("gpu-worker")
        assert {n.node_id for n in results} == {"n1"}

    @pytest.mark.asyncio
    async def test_out_edges(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1"))
        await g.add_node(_node("n2"))
        await g.add_node(_node("n3"))
        await g.add_edge(_edge("e1", "n1", "n2", "uses"))
        await g.add_edge(_edge("e2", "n1", "n3", "requires"))
        out = await g.out_edges("n1")
        assert len(out) == 2
        filtered = await g.out_edges("n1", relation="uses")
        assert len(filtered) == 1 and filtered[0].edge_id == "e1"

    @pytest.mark.asyncio
    async def test_in_edges(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1"))
        await g.add_node(_node("n2"))
        await g.add_edge(_edge("e1", "n1", "n2", "uses"))
        in_e = await g.in_edges("n2")
        assert len(in_e) == 1
        assert in_e[0].from_node_id == "n1"

    @pytest.mark.asyncio
    async def test_neighbors_out(self):
        g = KnowledgeGraph()
        await g.add_node(_node("n1"))
        await g.add_node(_node("n2"))
        await g.add_node(_node("n3"))
        await g.add_edge(_edge("e1", "n1", "n2"))
        await g.add_edge(_edge("e2", "n1", "n3"))
        neighbors = await g.neighbors("n1", direction="out")
        assert {n.node_id for n in neighbors} == {"n2", "n3"}

    @pytest.mark.asyncio
    async def test_query_by_type(self):
        g = KnowledgeGraph()
        await g.add_node(_node("w1", KnowledgeNodeType.WORKER))
        await g.add_node(_node("m1", KnowledgeNodeType.MODEL))
        results = await g.query(node_type=KnowledgeNodeType.MODEL)
        assert len(results) == 1
        assert results[0].node_id == "m1"

    @pytest.mark.asyncio
    async def test_query_property_filter(self):
        g = KnowledgeGraph()
        await g.add_node(_node("w1", KnowledgeNodeType.WORKER, region="us-east-1"))
        await g.add_node(_node("w2", KnowledgeNodeType.WORKER, region="eu-west-1"))
        results = await g.query(
            node_type=KnowledgeNodeType.WORKER,
            property_filter={"region": "us-east-1"},
        )
        assert {n.node_id for n in results} == {"w1"}

    @pytest.mark.asyncio
    async def test_count(self):
        g = KnowledgeGraph()
        assert await g.count() == (0, 0)
        await g.add_node(_node("n1"))
        await g.add_node(_node("n2"))
        await g.add_edge(_edge("e1", "n1", "n2"))
        nodes, edges = await g.count()
        assert nodes == 2
        assert edges == 1
