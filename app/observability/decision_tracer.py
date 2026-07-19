"""
app/observability/decision_tracer.py

Decision Traceability — answers "WHY did AEOS do X?" for every
consequential runtime decision.

Every decision record captures:
  - What was decided (action taken)
  - Why it was decided (triggering condition + reasoning)
  - What data informed it (inputs, observations)
  - Which policy applied (governance rule, invariant, SLO)
  - Which model/agent made it (if AI-assisted)
  - Confidence score and evidence
  - What alternatives were considered and rejected

Stored in append-only ring buffer (in-memory) + flushed to JSONL file
for postmortem analysis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class DecisionKind(str, Enum):
    SCHEDULING = "scheduling"          # Task→Worker assignment
    GOVERNANCE = "governance"          # Token issue/revoke/reject
    RECOVERY = "recovery"              # Fault recovery path chosen
    SCALING = "scaling"                # Scale up/down decision
    CIRCUIT_BREAKER = "circuit_breaker"  # Open/close/half-open
    CHECKPOINT = "checkpoint"          # Checkpoint trigger/skip
    REBALANCE = "rebalance"            # Partition/shard rebalance
    MODEL_INFERENCE = "model_inference"  # LLM/embedding call
    EVICTION = "eviction"              # Memory/cache eviction


@dataclass
class Alternative:
    """A rejected alternative that was considered."""
    description: str
    reason_rejected: str
    estimated_cost: float | None = None


@dataclass
class DecisionRecord:
    """
    Complete record of a single consequential decision.

    Every field except decision_id and timestamp is optional —
    fill in what you know, leave out what you don't.
    """
    decision_id: str
    kind: DecisionKind
    timestamp: float

    # What was decided
    action: str
    outcome_entity: str = ""          # task_id, node_id, etc.

    # Why it was decided
    trigger: str = ""                 # Event or condition that caused this
    reasoning: str = ""               # Human-readable explanation

    # What data informed it
    inputs: dict[str, Any] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)

    # Which policy / governance
    policy_id: str = ""               # e.g. "GOV-RULE-001", "INV-EXEC-001"
    invariants_checked: list[str] = field(default_factory=list)

    # AI attribution
    model_id: str = ""                # e.g. "claude-sonnet-4-6"
    agent_id: str = ""                # e.g. "scheduler-agent-1"
    confidence: float | None = None   # 0.0–1.0
    evidence: list[str] = field(default_factory=list)

    # Alternatives considered
    alternatives: list[Alternative] = field(default_factory=list)

    # Execution metadata
    latency_ms: float = 0.0
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "kind": self.kind.value,
            "timestamp": self.timestamp,
            "action": self.action,
            "outcome_entity": self.outcome_entity,
            "trigger": self.trigger,
            "reasoning": self.reasoning,
            "inputs": self.inputs,
            "observations": self.observations,
            "policy_id": self.policy_id,
            "invariants_checked": self.invariants_checked,
            "model_id": self.model_id,
            "agent_id": self.agent_id,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "alternatives": [
                {"description": a.description, "reason_rejected": a.reason_rejected}
                for a in self.alternatives
            ],
            "latency_ms": self.latency_ms,
            "tags": self.tags,
        }


class DecisionTracer:
    """
    Append-only decision tracer with in-memory ring buffer and JSONL persistence.

    Usage::

        tracer = DecisionTracer(output_dir="reports/decisions")

        record = tracer.record(
            kind=DecisionKind.SCHEDULING,
            action="dispatch task-123 to worker-5",
            outcome_entity="task-123",
            trigger="task_scheduled event",
            reasoning="worker-5 has lowest queue depth (2) and matches task affinity",
            inputs={"candidates": ["worker-3", "worker-4", "worker-5"],
                    "queue_depths": [8, 6, 2]},
            policy_id="SCHED-RULE-003",
            confidence=0.91,
        )
        # record is stored synchronously; JSONL flush is async
    """

    def __init__(
        self,
        output_dir: str = "reports/decisions",
        ring_buffer_size: int = 10_000,
        flush_interval: float = 10.0,
    ) -> None:
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._buffer: deque[DecisionRecord] = deque(maxlen=ring_buffer_size)
        self._flush_interval = flush_interval
        self._unflushed: list[DecisionRecord] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._jsonl_path = self._output_dir / f"decisions-{int(time.time())}.jsonl"

    def record(
        self,
        kind: DecisionKind,
        action: str,
        *,
        outcome_entity: str = "",
        trigger: str = "",
        reasoning: str = "",
        inputs: dict[str, Any] | None = None,
        observations: list[dict[str, Any]] | None = None,
        policy_id: str = "",
        invariants_checked: list[str] | None = None,
        model_id: str = "",
        agent_id: str = "",
        confidence: float | None = None,
        evidence: list[str] | None = None,
        alternatives: list[Alternative] | None = None,
        latency_ms: float = 0.0,
        tags: list[str] | None = None,
    ) -> DecisionRecord:
        """
        Record a decision synchronously. Returns the record immediately.
        JSONL persistence happens asynchronously in the background.
        """
        rec = DecisionRecord(
            decision_id=str(uuid.uuid4()),
            kind=kind,
            timestamp=time.time(),
            action=action,
            outcome_entity=outcome_entity,
            trigger=trigger,
            reasoning=reasoning,
            inputs=inputs or {},
            observations=observations or [],
            policy_id=policy_id,
            invariants_checked=invariants_checked or [],
            model_id=model_id,
            agent_id=agent_id,
            confidence=confidence,
            evidence=evidence or [],
            alternatives=alternatives or [],
            latency_ms=latency_ms,
            tags=tags or [],
        )
        self._buffer.append(rec)
        self._unflushed.append(rec)
        logger.debug(
            "[DecisionTracer] %s: %s → %s",
            kind.value, outcome_entity or "system", action[:80],
        )
        return rec

    async def start(self) -> None:
        """Start background JSONL flush loop."""
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("DecisionTracer started; writing to %s", self._jsonl_path)

    async def stop(self) -> None:
        """Stop flush loop and write remaining records."""
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self._flush_to_disk()
        logger.info("DecisionTracer stopped; %d total decisions recorded", len(self._buffer))

    async def flush(self) -> None:
        """Manually flush pending records to disk."""
        await self._flush_to_disk()

    def recent(self, n: int = 100, kind: DecisionKind | None = None) -> list[DecisionRecord]:
        """Return the N most recent decisions, optionally filtered by kind."""
        records = list(self._buffer)
        if kind is not None:
            records = [r for r in records if r.kind == kind]
        return records[-n:]

    def query(
        self,
        entity_id: str | None = None,
        kind: DecisionKind | None = None,
        since: float | None = None,
        policy_id: str | None = None,
    ) -> list[DecisionRecord]:
        """Query decisions from the ring buffer by various filters."""
        results = list(self._buffer)
        if entity_id:
            results = [r for r in results if r.outcome_entity == entity_id]
        if kind:
            results = [r for r in results if r.kind == kind]
        if since:
            results = [r for r in results if r.timestamp >= since]
        if policy_id:
            results = [r for r in results if r.policy_id == policy_id]
        return results

    def explain(self, entity_id: str) -> str:
        """
        Generate a human-readable explanation of all decisions affecting an entity.
        Useful for postmortem generation.
        """
        decisions = self.query(entity_id=entity_id)
        if not decisions:
            return f"No decision records found for entity '{entity_id}'"

        lines = [f"Decision trace for '{entity_id}' ({len(decisions)} records):"]
        for rec in decisions:
            lines.append(
                f"\n  [{rec.kind.value.upper()}] {time.strftime('%H:%M:%S', time.localtime(rec.timestamp))}"
                f"\n    Action: {rec.action}"
            )
            if rec.trigger:
                lines.append(f"    Trigger: {rec.trigger}")
            if rec.reasoning:
                lines.append(f"    Reason: {rec.reasoning}")
            if rec.policy_id:
                lines.append(f"    Policy: {rec.policy_id}")
            if rec.confidence is not None:
                lines.append(f"    Confidence: {rec.confidence:.0%}")
            if rec.alternatives:
                lines.append(f"    Alternatives rejected: {len(rec.alternatives)}")
                for alt in rec.alternatives[:3]:
                    lines.append(f"      - {alt.description}: {alt.reason_rejected}")
        return "\n".join(lines)

    async def _flush_to_disk(self) -> None:
        async with self._lock:
            if not self._unflushed:
                return
            to_write = self._unflushed[:]
            self._unflushed.clear()

        try:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                for rec in to_write:
                    f.write(json.dumps(rec.to_dict()) + "\n")
        except OSError as exc:
            logger.error("DecisionTracer flush error: %s", exc)
            # Re-queue on failure
            self._unflushed = to_write + self._unflushed

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._flush_interval)
                await self._flush_to_disk()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("DecisionTracer flush loop error: %s", exc)
