"""
app/observability/__init__.py

Platform Observability 2.0 — P12A.3

Three pillars:
  1. RuntimeGraph    — real-time topology of all live entities and relationships
  2. DecisionTracer  — why every consequential decision was made
  3. DistributedTimeline — causal event ordering for <60s postmortem generation
"""

from .runtime_graph import RuntimeGraph, GraphNode, GraphEdge, NodeKind, EdgeKind
from .decision_tracer import DecisionTracer, DecisionRecord, DecisionKind, Alternative
from .distributed_timeline import (
    DistributedTimeline,
    TimelineEvent,
    Postmortem,
)

__all__ = [
    "RuntimeGraph",
    "GraphNode",
    "GraphEdge",
    "NodeKind",
    "EdgeKind",
    "DecisionTracer",
    "DecisionRecord",
    "DecisionKind",
    "Alternative",
    "DistributedTimeline",
    "TimelineEvent",
    "Postmortem",
]
