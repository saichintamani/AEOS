"""
app/observability/distributed_timeline.py

Distributed Event Timeline — causal ordering of events across all nodes
for sub-60-second postmortem generation.

Architecture:
  - TimelineEvent: a timestamped, causally-linked event from any node
  - DistributedTimeline: ingests events, sorts by Lamport clock, exports
    structured postmortem within 60 seconds of an incident signal

Causal chain reconstruction uses vector clocks (simplified: Lamport scalar
per node) to determine happens-before relationships.

Used by:
  - Chaos experiment reports (what happened during fault injection)
  - Incident response (reconstruct failure timeline in <60s)
  - Compliance audit (immutable record of system decisions)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TimelineEvent:
    """
    A single event in the distributed timeline.

    lamport_clock: logical clock value at the emitting node
    vector_clock: per-node Lamport clocks at time of emission
    cause_event_id: ID of the event that caused this one (if known)
    """
    event_id: str
    node_id: str
    event_type: str
    timestamp: float              # Wall clock (UTC epoch seconds)
    lamport_clock: int            # Logical clock at emitting node
    vector_clock: dict[str, int] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)
    cause_event_id: str | None = None   # Causal parent
    tags: list[str] = field(default_factory=list)

    def happens_before(self, other: "TimelineEvent") -> bool:
        """
        Returns True if self happened-before other per vector clock.
        Uses partial order: self.vc[n] ≤ other.vc[n] for all n,
        with at least one strict inequality.
        """
        if not self.vector_clock or not other.vector_clock:
            # Fall back to wall clock comparison
            return self.timestamp < other.timestamp

        all_nodes = set(self.vector_clock) | set(other.vector_clock)
        leq = all(
            self.vector_clock.get(n, 0) <= other.vector_clock.get(n, 0)
            for n in all_nodes
        )
        strict = any(
            self.vector_clock.get(n, 0) < other.vector_clock.get(n, 0)
            for n in all_nodes
        )
        return leq and strict

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "node_id": self.node_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "lamport_clock": self.lamport_clock,
            "vector_clock": self.vector_clock,
            "payload": self.payload,
            "cause_event_id": self.cause_event_id,
            "tags": self.tags,
        }


@dataclass
class PostmortemSection:
    heading: str
    content: str


@dataclass
class Postmortem:
    """Structured postmortem generated from a timeline slice."""
    incident_id: str
    generated_at: float
    incident_start: float
    incident_end: float
    duration_seconds: float
    event_count: int
    sections: list[PostmortemSection]
    raw_timeline: list[TimelineEvent]

    def to_markdown(self) -> str:
        lines = [
            f"# Postmortem: {self.incident_id}",
            f"",
            f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(self.generated_at))}  ",
            f"**Duration:** {self.duration_seconds:.1f}s  ",
            f"**Events:** {self.event_count}  ",
            f"",
        ]
        for section in self.sections:
            lines.append(f"## {section.heading}")
            lines.append(section.content)
            lines.append("")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "incident_id": self.incident_id,
            "generated_at": self.generated_at,
            "incident_start": self.incident_start,
            "incident_end": self.incident_end,
            "duration_seconds": self.duration_seconds,
            "event_count": self.event_count,
            "sections": [{"heading": s.heading, "content": s.content} for s in self.sections],
        }


class DistributedTimeline:
    """
    Collects events from all nodes, reconstructs causal order,
    and generates structured postmortems in under 60 seconds.

    Usage::

        timeline = DistributedTimeline(output_dir="reports/timelines")
        timeline.ingest(event)
        postmortem = await timeline.generate_postmortem(
            incident_id="INC-2026-001",
            window_start=t0,
            window_end=t1,
        )
        print(postmortem.to_markdown())
    """

    def __init__(self, output_dir: str = "reports/timelines") -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._events: list[TimelineEvent] = []
        self._by_node: dict[str, list[TimelineEvent]] = defaultdict(list)
        self._by_type: dict[str, list[TimelineEvent]] = defaultdict(list)
        self._causal_graph: dict[str, list[str]] = defaultdict(list)  # cause→effects
        self._lock = asyncio.Lock()

    async def ingest(self, event: TimelineEvent) -> None:
        """Add an event to the timeline."""
        async with self._lock:
            self._events.append(event)
            self._by_node[event.node_id].append(event)
            self._by_type[event.event_type].append(event)
            if event.cause_event_id:
                self._causal_graph[event.cause_event_id].append(event.event_id)

    def ingest_sync(self, event: TimelineEvent) -> None:
        """Synchronous ingest for use outside async context."""
        self._events.append(event)
        self._by_node[event.node_id].append(event)
        self._by_type[event.event_type].append(event)
        if event.cause_event_id:
            self._causal_graph[event.cause_event_id].append(event.event_id)

    async def ingest_from_kafka(self, topic: str, message: dict[str, Any], offset: int) -> None:
        """Convert a Kafka message to a TimelineEvent and ingest it."""
        event = TimelineEvent(
            event_id=message.get("event_id", f"{topic}-{offset}"),
            node_id=message.get("node_id", message.get("actor", "unknown")),
            event_type=message.get("event_type", "unknown"),
            timestamp=message.get("timestamp", time.time()),
            lamport_clock=message.get("lamport_clock", 0),
            vector_clock=message.get("vector_clock", {}),
            payload={k: v for k, v in message.items()
                     if k not in ("event_id", "node_id", "event_type", "timestamp",
                                  "lamport_clock", "vector_clock", "cause_event_id")},
            cause_event_id=message.get("cause_event_id"),
            tags=message.get("tags", []),
        )
        await self.ingest(event)

    def slice(
        self,
        window_start: float,
        window_end: float,
        node_id: str | None = None,
        event_types: list[str] | None = None,
    ) -> list[TimelineEvent]:
        """
        Extract events within a time window, sorted by causal order
        (vector clock) with wall clock as tiebreaker.
        """
        filtered = [
            e for e in self._events
            if window_start <= e.timestamp <= window_end
            and (node_id is None or e.node_id == node_id)
            and (event_types is None or e.event_type in event_types)
        ]
        # Sort by Lamport clock first, then wall clock
        filtered.sort(key=lambda e: (e.lamport_clock, e.timestamp))
        return filtered

    async def generate_postmortem(
        self,
        incident_id: str,
        window_start: float,
        window_end: float,
    ) -> Postmortem:
        """
        Generate a structured postmortem for an incident window.
        Target: completes in under 60 seconds.
        """
        gen_start = time.monotonic()
        logger.info("Generating postmortem for %s (%.0fs window)", incident_id,
                    window_end - window_start)

        events = self.slice(window_start, window_end)

        sections = await asyncio.gather(
            asyncio.to_thread(self._section_summary, events, window_start, window_end),
            asyncio.to_thread(self._section_timeline, events),
            asyncio.to_thread(self._section_root_cause, events),
            asyncio.to_thread(self._section_impact, events),
            asyncio.to_thread(self._section_contributing_factors, events),
            asyncio.to_thread(self._section_action_items, events),
        )

        postmortem = Postmortem(
            incident_id=incident_id,
            generated_at=time.time(),
            incident_start=window_start,
            incident_end=window_end,
            duration_seconds=window_end - window_start,
            event_count=len(events),
            sections=list(sections),
            raw_timeline=events,
        )

        gen_elapsed = (time.monotonic() - gen_start) * 1000
        logger.info("Postmortem generated in %.1fms", gen_elapsed)

        # Save to disk
        path = self._output_dir / f"{incident_id}-postmortem.md"
        path.write_text(postmortem.to_markdown(), encoding="utf-8")
        json_path = self._output_dir / f"{incident_id}-postmortem.json"
        json_path.write_text(json.dumps(postmortem.to_dict(), indent=2), encoding="utf-8")

        return postmortem

    # ── Section builders (run in thread pool) ─────────────────────────────

    def _section_summary(
        self,
        events: list[TimelineEvent],
        window_start: float,
        window_end: float,
    ) -> PostmortemSection:
        duration = window_end - window_start
        node_ids = {e.node_id for e in events}
        event_type_counts: dict[str, int] = defaultdict(int)
        for e in events:
            event_type_counts[e.event_type] += 1

        top_types = sorted(event_type_counts.items(), key=lambda x: -x[1])[:5]
        content = (
            f"- **Duration:** {duration:.1f}s\n"
            f"- **Nodes involved:** {len(node_ids)} ({', '.join(sorted(node_ids)[:5])})\n"
            f"- **Total events:** {len(events)}\n"
            f"- **Top event types:** "
            + ", ".join(f"{t} ({c})" for t, c in top_types)
        )
        return PostmortemSection(heading="Summary", content=content)

    def _section_timeline(self, events: list[TimelineEvent]) -> PostmortemSection:
        lines = ["| Time | Node | Event | Details |", "|------|------|-------|---------|"]
        for e in events[:50]:  # First 50 events
            ts = time.strftime("%H:%M:%S", time.gmtime(e.timestamp))
            detail = ", ".join(f"{k}={v}" for k, v in list(e.payload.items())[:3])
            lines.append(f"| {ts} | {e.node_id} | {e.event_type} | {detail[:60]} |")
        if len(events) > 50:
            lines.append(f"| ... | ... | *{len(events) - 50} more events* | ... |")
        return PostmortemSection(heading="Event Timeline", content="\n".join(lines))

    def _section_root_cause(self, events: list[TimelineEvent]) -> PostmortemSection:
        # Find first failure event as candidate root cause
        failure_types = {"task_failed", "node_failure_confirmed", "checkpoint_failed",
                         "governance_rejected", "raft_election_timeout"}
        failures = [e for e in events if e.event_type in failure_types]

        if not failures:
            content = "No failure events detected in this window."
        else:
            first = failures[0]
            ts = time.strftime("%H:%M:%S", time.gmtime(first.timestamp))
            # Trace causal chain
            chain = self._trace_causal_chain(first.event_id, events)
            content = (
                f"**First failure:** `{first.event_type}` at {ts} on node `{first.node_id}`\n\n"
                f"**Payload:** `{json.dumps(first.payload, default=str)[:200]}`\n\n"
                f"**Causal chain ({len(chain)} events):**\n"
            )
            for eid in chain[:10]:
                evt = next((e for e in events if e.event_id == eid), None)
                if evt:
                    content += f"  → `{evt.event_type}` ({evt.node_id})\n"
        return PostmortemSection(heading="Root Cause Analysis", content=content)

    def _section_impact(self, events: list[TimelineEvent]) -> PostmortemSection:
        failed_tasks = {e.payload.get("task_id") for e in events
                        if e.event_type == "task_failed" and e.payload.get("task_id")}
        failed_nodes = {e.payload.get("node_id") for e in events
                        if e.event_type == "node_failure_confirmed" and e.payload.get("node_id")}
        completed = sum(1 for e in events if e.event_type == "task_completed")

        content = (
            f"- **Tasks failed:** {len(failed_tasks)}\n"
            f"- **Tasks completed during incident:** {completed}\n"
            f"- **Nodes lost:** {len(failed_nodes)} ({', '.join(str(n) for n in failed_nodes)})\n"
        )
        return PostmortemSection(heading="Impact", content=content)

    def _section_contributing_factors(self, events: list[TimelineEvent]) -> PostmortemSection:
        factors = []
        # Clock skew
        if any(e.event_type == "clock_skew_detected" for e in events):
            factors.append("- Clock skew detected between nodes")
        # Network partition
        if any(e.event_type in ("network_partition_start", "raft_election_started") for e in events):
            factors.append("- Network instability / partition detected")
        # Governance delays
        gov_delays = [e for e in events if e.event_type == "governance_timeout"]
        if gov_delays:
            factors.append(f"- Governance service delays ({len(gov_delays)} timeouts)")
        # Storage pressure
        if any(e.event_type == "disk_pressure_critical" for e in events):
            factors.append("- Disk pressure reached critical threshold")
        if not factors:
            factors.append("- No obvious contributing factors identified from event stream")
        return PostmortemSection(
            heading="Contributing Factors",
            content="\n".join(factors),
        )

    def _section_action_items(self, events: list[TimelineEvent]) -> PostmortemSection:
        items = [
            "- [ ] Verify RTO met for all affected services",
            "- [ ] Confirm no exactly-once violations via replay validator",
            "- [ ] Review governance audit log for unauthorized bypasses",
            "- [ ] Update chaos experiment baselines with observed recovery times",
            "- [ ] File incident report in linear project AEOS-OPS",
        ]
        return PostmortemSection(heading="Action Items", content="\n".join(items))

    def _trace_causal_chain(
        self, event_id: str, events: list[TimelineEvent]
    ) -> list[str]:
        """Follow cause_event_id links to build a causal chain."""
        chain = []
        event_map = {e.event_id: e for e in events}
        current_id: str | None = event_id
        visited: set[str] = set()
        while current_id and current_id not in visited and len(chain) < 20:
            visited.add(current_id)
            chain.append(current_id)
            evt = event_map.get(current_id)
            current_id = evt.cause_event_id if evt else None
        return chain

    def export_jsonl(self, path: str, window_start: float | None = None,
                     window_end: float | None = None) -> int:
        """Export timeline events as JSONL. Returns number of events written."""
        events = self._events
        if window_start is not None:
            events = [e for e in events if e.timestamp >= window_start]
        if window_end is not None:
            events = [e for e in events if e.timestamp <= window_end]

        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e.to_dict()) + "\n")
        return len(events)
