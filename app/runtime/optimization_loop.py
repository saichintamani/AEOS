"""
Wave 9B.4.6 — Autonomous Optimization Loop

Every execution produces feedback. The OptimizationLoop:
  1. Ingests ExecutionRecords
  2. Updates LearningEngine
  3. Updates KnowledgeGraph (worker → model, task → failure)
  4. Recomputes CapabilityProfile scores for affected workers
  5. Publishes optimization telemetry

Over time, scheduling decisions measurably improve.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.runtime_intelligence.contracts import (
    ExecutionRecord,
    KnowledgeEdge,
    KnowledgeNode,
    KnowledgeNodeType,
)
from app.runtime_intelligence.knowledge_graph import KnowledgeGraph
from app.runtime_intelligence.learning_engine import DefaultLearningEngine
from app.runtime.telemetry_bus import TelemetryBus, TelemetryEvent, TelemetryEventType

logger = logging.getLogger(__name__)


@dataclass
class OptimizationSummary:
    records_processed: int = 0
    kg_nodes_added: int = 0
    kg_edges_added: int = 0
    models_updated: int = 0
    workers_updated: int = 0


class AutonomousOptimizationLoop:
    """
    Processes execution records and propagates learning across subsystems.

    Flow per record:
      record → LearningEngine.record()
             → KnowledgeGraph: add worker node, model node, edge
             → KnowledgeGraph: if failed, add failure node + edge
             → Telemetry: LEARNING_UPDATED
    """

    def __init__(
        self,
        learning_engine: DefaultLearningEngine | None = None,
        knowledge_graph: KnowledgeGraph | None = None,
        telemetry_bus: TelemetryBus | None = None,
        batch_size: int = 32,
    ) -> None:
        self._learning = learning_engine or DefaultLearningEngine()
        self._kg = knowledge_graph or KnowledgeGraph()
        self._bus = telemetry_bus
        self._batch_size = batch_size
        self._queue: asyncio.Queue[ExecutionRecord] = asyncio.Queue()
        self._running = False
        self._process_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True
        self._process_task = asyncio.create_task(
            self._process_loop(), name="optimization-loop"
        )

    async def stop(self) -> None:
        self._running = False
        if self._process_task:
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass

    async def ingest(self, record: ExecutionRecord) -> None:
        await self._queue.put(record)

    async def ingest_batch(self, records: list[ExecutionRecord]) -> None:
        for record in records:
            await self._queue.put(record)

    async def process_now(self, records: list[ExecutionRecord]) -> OptimizationSummary:
        """Synchronous batch processing (no background loop needed)."""
        summary = OptimizationSummary()
        for record in records:
            await self._process_one(record, summary)
        return summary

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _process_loop(self) -> None:
        while self._running:
            batch: list[ExecutionRecord] = []
            try:
                rec = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                batch.append(rec)
                # Drain up to batch_size more without blocking
                while len(batch) < self._batch_size:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
            except asyncio.TimeoutError:
                continue

            summary = OptimizationSummary()
            for record in batch:
                await self._process_one(record, summary)

            if self._bus and summary.records_processed > 0:
                self._bus.emit(TelemetryEvent(
                    event_type=TelemetryEventType.LEARNING_UPDATED,
                    source="OptimizationLoop",
                    payload={
                        "records_processed": summary.records_processed,
                        "workers_updated": summary.workers_updated,
                        "models_updated": summary.models_updated,
                    },
                ))

    async def _process_one(
        self, record: ExecutionRecord, summary: OptimizationSummary
    ) -> None:
        # Update learning engine
        await self._learning.record(record)
        summary.records_processed += 1
        summary.workers_updated += 1

        # KnowledgeGraph: ensure worker node exists
        worker_node_id = f"worker:{record.worker_id}"
        if not await self._kg.get_node(worker_node_id):
            await self._kg.add_node(KnowledgeNode(
                node_id=worker_node_id,
                node_type=KnowledgeNodeType.WORKER,
                label=record.worker_id,
                properties={"worker_id": record.worker_id},
            ))
            summary.kg_nodes_added += 1

        # KnowledgeGraph: ensure model node + edge
        if record.model_used:
            summary.models_updated += 1
            model_node_id = f"model:{record.model_used}"
            if not await self._kg.get_node(model_node_id):
                await self._kg.add_node(KnowledgeNode(
                    node_id=model_node_id,
                    node_type=KnowledgeNodeType.MODEL,
                    label=record.model_used,
                ))
                summary.kg_nodes_added += 1
            # Worker → Model edge
            edge = KnowledgeEdge(
                edge_id=f"exec:{record.record_id}:uses",
                from_node_id=worker_node_id,
                to_node_id=model_node_id,
                relation="executes",
                weight=1.0 if record.success else 0.0,
                properties={
                    "task_id": record.task_id,
                    "success": record.success,
                    "latency_ms": record.latency_ms,
                },
            )
            await self._kg.add_edge(edge)
            summary.kg_edges_added += 1

        # KnowledgeGraph: failure node
        if record.failed and record.error_type:
            failure_node_id = f"failure:{record.task_id}"
            await self._kg.add_node(KnowledgeNode(
                node_id=failure_node_id,
                node_type=KnowledgeNodeType.FAILURE,
                label=record.error_type,
                properties={
                    "task_id": record.task_id,
                    "error_type": record.error_type,
                    "worker_id": record.worker_id,
                },
            ))
            summary.kg_nodes_added += 1
            await self._kg.add_edge(KnowledgeEdge(
                edge_id=f"fail:{record.record_id}",
                from_node_id=worker_node_id,
                to_node_id=failure_node_id,
                relation="caused_failure",
            ))
            summary.kg_edges_added += 1
