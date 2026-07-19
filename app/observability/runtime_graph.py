"""
app/observability/runtime_graph.py

Runtime Topology Graph — real-time directed graph of all live AEOS entities
and their relationships.

Nodes:
  - Workflow (PENDING | RUNNING | COMPLETED | FAILED)
  - Task (PENDING | SCHEDULED | RUNNING | SUSPENDED | COMPLETED | FAILED)
  - Worker (JOINING | RUNNING | DRAINING | LEFT | FAILED)
  - Agent (IDLE | BUSY | ERROR)
  - MemoryStore (ACTIVE | DEGRADED)
  - Checkpoint (PENDING | COMMITTED | FAILED)

Edges:
  - workflow → task (contains)
  - task → worker (dispatched_to)
  - task → checkpoint (protected_by)
  - task → task (depends_on)
  - worker → agent (runs)
  - agent → memory_store (reads_from)

Updated in real time via Kafka event subscription.
Exported as adjacency JSON for the dashboard WebSocket feed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class NodeKind(str, Enum):
    WORKFLOW = "workflow"
    TASK = "task"
    WORKER = "worker"
    AGENT = "agent"
    MEMORY_STORE = "memory_store"
    CHECKPOINT = "checkpoint"


class EdgeKind(str, Enum):
    CONTAINS = "contains"
    DISPATCHED_TO = "dispatched_to"
    PROTECTED_BY = "protected_by"
    DEPENDS_ON = "depends_on"
    RUNS = "runs"
    READS_FROM = "reads_from"


@dataclass
class GraphNode:
    node_id: str
    kind: NodeKind
    state: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "state": self.state,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class GraphEdge:
    source_id: str
    target_id: str
    kind: EdgeKind
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def edge_id(self) -> str:
        return f"{self.source_id}-[{self.kind.value}]->{self.target_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": self.kind.value,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }


class RuntimeGraph:
    """
    Thread-safe, in-memory runtime topology graph.

    Subscribes to Kafka events and maintains a live view of:
      - All workflows, tasks, workers, agents, checkpoints
      - Their current states
      - Their relationships (edges)

    Provides snapshot export for dashboard WebSocket feeds.

    Usage::

        graph = RuntimeGraph(kafka_consumer)
        await graph.start()
        snapshot = graph.snapshot()  # For dashboard
        await graph.stop()
    """

    def __init__(self, kafka_consumer: Any | None = None) -> None:
        self._consumer = kafka_consumer
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info("RuntimeGraph started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("RuntimeGraph stopped: %d nodes, %d edges", self.node_count, self.edge_count)

    async def upsert_node(
        self,
        node_id: str,
        kind: NodeKind,
        state: str,
        metadata: dict[str, Any] | None = None,
    ) -> GraphNode:
        async with self._lock:
            if node_id in self._nodes:
                node = self._nodes[node_id]
                node.state = state
                node.updated_at = time.time()
                if metadata:
                    node.metadata.update(metadata)
            else:
                node = GraphNode(
                    node_id=node_id,
                    kind=kind,
                    state=state,
                    metadata=metadata or {},
                )
                self._nodes[node_id] = node
            return node

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        kind: EdgeKind,
        metadata: dict[str, Any] | None = None,
    ) -> GraphEdge:
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            kind=kind,
            metadata=metadata or {},
        )
        async with self._lock:
            self._edges[edge.edge_id] = edge
        return edge

    async def remove_node(self, node_id: str) -> None:
        async with self._lock:
            self._nodes.pop(node_id, None)
            # Remove all edges involving this node
            stale = [eid for eid, e in self._edges.items()
                     if e.source_id == node_id or e.target_id == node_id]
            for eid in stale:
                del self._edges[eid]

    def snapshot(self) -> dict[str, Any]:
        """Export current graph as JSON-serializable dict."""
        return {
            "timestamp": time.time(),
            "node_count": len(self._nodes),
            "edge_count": len(self._edges),
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges.values()],
        }

    def nodes_by_kind(self, kind: NodeKind) -> list[GraphNode]:
        return [n for n in self._nodes.values() if n.kind == kind]

    def nodes_by_state(self, state: str) -> list[GraphNode]:
        return [n for n in self._nodes.values() if n.state == state]

    def neighbors(self, node_id: str) -> list[str]:
        """Return IDs of all nodes directly connected to node_id."""
        return list({
            e.target_id if e.source_id == node_id else e.source_id
            for e in self._edges.values()
            if e.source_id == node_id or e.target_id == node_id
        })

    async def process_event(self, topic: str, message: dict[str, Any]) -> None:
        """Process a single Kafka event and update graph state."""
        et = message.get("event_type", "")

        # ── Task events ───────────────────────────────────────────────────
        if et == "task_created":
            task_id = message.get("task_id", "")
            workflow_id = message.get("workflow_id", "")
            await self.upsert_node(task_id, NodeKind.TASK, "PENDING", {"workflow_id": workflow_id})
            if workflow_id:
                await self.upsert_node(workflow_id, NodeKind.WORKFLOW, "RUNNING")
                await self.add_edge(workflow_id, task_id, EdgeKind.CONTAINS)

        elif et == "task_scheduled":
            task_id = message.get("task_id", "")
            worker_id = message.get("worker_id", "")
            await self.upsert_node(task_id, NodeKind.TASK, "SCHEDULED")
            if worker_id:
                await self.upsert_node(worker_id, NodeKind.WORKER, "RUNNING")
                await self.add_edge(task_id, worker_id, EdgeKind.DISPATCHED_TO)

        elif et == "execution_started":
            await self.upsert_node(message.get("task_id", ""), NodeKind.TASK, "RUNNING")

        elif et == "task_completed":
            await self.upsert_node(message.get("task_id", ""), NodeKind.TASK, "COMPLETED")

        elif et == "task_failed":
            await self.upsert_node(message.get("task_id", ""), NodeKind.TASK, "FAILED")

        elif et == "task_suspended":
            await self.upsert_node(message.get("task_id", ""), NodeKind.TASK, "SUSPENDED")

        elif et == "task_resumed":
            await self.upsert_node(message.get("task_id", ""), NodeKind.TASK, "RUNNING")

        # ── Checkpoint events ─────────────────────────────────────────────
        elif et == "checkpoint_phase1_written":
            ckpt_id = message.get("checkpoint_id", f"ckpt-{message.get('task_id', '')}")
            task_id = message.get("task_id", "")
            await self.upsert_node(ckpt_id, NodeKind.CHECKPOINT, "PENDING")
            if task_id:
                await self.add_edge(task_id, ckpt_id, EdgeKind.PROTECTED_BY)

        elif et == "checkpoint_committed":
            ckpt_id = message.get("checkpoint_id", f"ckpt-{message.get('task_id', '')}")
            await self.upsert_node(ckpt_id, NodeKind.CHECKPOINT, "COMMITTED")

        # ── Worker/node events ────────────────────────────────────────────
        elif et == "node_join_complete":
            await self.upsert_node(
                message.get("node_id", ""), NodeKind.WORKER, "RUNNING",
                {"address": message.get("address", "")},
            )

        elif et == "node_drain_complete":
            await self.upsert_node(message.get("node_id", ""), NodeKind.WORKER, "LEFT")

        elif et == "node_failure_confirmed":
            await self.upsert_node(message.get("node_id", ""), NodeKind.WORKER, "FAILED")

        # ── Workflow events ───────────────────────────────────────────────
        elif et == "workflow_completed":
            await self.upsert_node(message.get("workflow_id", ""), NodeKind.WORKFLOW, "COMPLETED")

        elif et == "workflow_failed":
            await self.upsert_node(message.get("workflow_id", ""), NodeKind.WORKFLOW, "FAILED")

    async def _consume_loop(self) -> None:
        if self._consumer is None:
            logger.warning("RuntimeGraph: no Kafka consumer — graph will not auto-update")
            return

        topics = ["aeos.tasks", "aeos.membership", "aeos.checkpoints", "aeos.workflows"]
        while self._running:
            try:
                records = await asyncio.wait_for(
                    self._consumer.getmany(timeout_ms=1000), timeout=2.0
                )
                for tp, messages in records.items():
                    for msg in messages:
                        try:
                            payload = json.loads(msg.value)
                            await self.process_event(tp.topic, payload)
                        except Exception as exc:
                            logger.debug("RuntimeGraph event error: %s", exc)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("RuntimeGraph consumer error: %s", exc)
                await asyncio.sleep(1.0)
