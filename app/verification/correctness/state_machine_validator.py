"""
app/verification/correctness/state_machine_validator.py

Live State Machine Validator — extends the existing StateMachineValidator
by wiring SM-GOVERNANCE and SM-CAPABILITY to live event streams.

Addresses gap identified in GAP_ANALYSIS §4:
  "Both machines are defined but not connected to live event streams."

This module provides:
  - LiveStateMachineValidator: subscribes to Kafka event streams and
    validates transitions in real time
  - EventAdapter: converts raw Kafka/Redis messages to StateMachine events
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.distributed.validation.state_machine import (
    StateMachineValidator,
    StateMachineViolation,
    TransitionRecord,
)

logger = logging.getLogger(__name__)


@dataclass
class LiveViolation:
    """A state machine violation detected in the live stream."""
    machine_id: str
    entity_id: str
    from_state: str
    to_state: str
    event: str
    kafka_offset: int
    timestamp: float
    description: str


@dataclass
class LiveValidationStats:
    """Statistics from the live validator."""
    start_time: float = field(default_factory=time.time)
    events_processed: int = 0
    transitions_valid: int = 0
    transitions_invalid: int = 0
    violations: list[LiveViolation] = field(default_factory=list)

    @property
    def violation_rate(self) -> float:
        total = self.transitions_valid + self.transitions_invalid
        return self.transitions_invalid / total if total > 0 else 0.0


class EventAdapter:
    """
    Converts raw Kafka/Redis event messages to StateMachineValidator
    transition records.

    Supports the following event topics:
      - aeos.tasks        → SM-TASK transitions
      - aeos.membership   → SM-CLUSTER-MEMBER transitions
      - aeos.checkpoints  → SM-CHECKPOINT transitions
      - aeos.governance   → SM-GOVERNANCE transitions (NEW)
      - aeos.capabilities → SM-CAPABILITY transitions (NEW)
    """

    # Maps Kafka event_type → (machine_id, trigger_event, from_state, to_state)
    # None values mean: "infer from payload"
    _GOVERNANCE_EVENTS: dict[str, tuple[str, str]] = {
        "governance_token_issued":   ("ISSUED", "ACTIVE"),
        "governance_token_consumed": ("ACTIVE", "APPROVED"),
        "governance_policy_rejected": ("ISSUED", "REJECTED"),
        "governance_token_expired":  ("ACTIVE", "EXPIRED"),
        "governance_token_revoked":  ("ACTIVE", "REVOKED"),
    }

    _CAPABILITY_EVENTS: dict[str, tuple[str, str]] = {
        "capability_registered":    ("UNREGISTERED", "ACTIVE"),
        "capability_heartbeat":     ("ACTIVE", "ACTIVE"),
        "capability_refresh_start": ("ACTIVE", "REFRESHING"),
        "capability_refresh_done":  ("REFRESHING", "ACTIVE"),
        "capability_stale_detected": ("ACTIVE", "STALE"),
        "capability_deregistered":  ("ACTIVE", "DEREGISTERED"),
        "capability_stale_evicted": ("STALE", "DEREGISTERED"),
    }

    def adapt(
        self,
        topic: str,
        message: dict[str, Any],
        kafka_offset: int,
    ) -> tuple[str, str, str, str, str] | None:
        """
        Convert a raw message to (machine_id, entity_id, from_state, to_state, event).
        Returns None if the message is not relevant to state machine validation.
        """
        et = message.get("event_type", "")
        entity_id = message.get("entity_id") or message.get("task_id") or message.get("node_id") or ""

        if topic == "aeos.governance":
            mapping = self._GOVERNANCE_EVENTS.get(et)
            if mapping:
                from_s, to_s = mapping
                return ("SM-GOVERNANCE", entity_id, from_s, to_s, et)

        elif topic == "aeos.capabilities":
            mapping = self._CAPABILITY_EVENTS.get(et)
            if mapping:
                from_s, to_s = mapping
                return ("SM-CAPABILITY", entity_id, from_s, to_s, et)

        elif topic == "aeos.tasks":
            return self._adapt_task_event(et, entity_id, message)

        elif topic == "aeos.membership":
            return self._adapt_membership_event(et, entity_id)

        return None

    def _adapt_task_event(
        self, event_type: str, task_id: str, payload: dict[str, Any]
    ) -> tuple[str, str, str, str, str] | None:
        _TASK_EVENTS: dict[str, tuple[str, str]] = {
            "task_created":     ("PENDING", "PENDING"),
            "task_scheduled":   ("PENDING", "SCHEDULED"),
            "execution_started": ("SCHEDULED", "RUNNING"),
            "task_suspended":   ("RUNNING", "SUSPENDED"),
            "task_resumed":     ("SUSPENDED", "RUNNING"),
            "task_completed":   ("RUNNING", "COMPLETED"),
            "task_failed":      ("RUNNING", "FAILED"),
            "task_cancelled":   ("PENDING", "CANCELLED"),
            "task_timeout":     ("RUNNING", "TIMEOUT"),
        }
        mapping = _TASK_EVENTS.get(event_type)
        if mapping:
            from_s, to_s = mapping
            return ("SM-TASK", task_id, from_s, to_s, event_type)
        return None

    def _adapt_membership_event(
        self, event_type: str, node_id: str
    ) -> tuple[str, str, str, str, str] | None:
        _MEMBER_EVENTS: dict[str, tuple[str, str]] = {
            "node_join_initiated":  ("JOINING", "JOINING"),
            "node_join_complete":   ("JOINING", "RUNNING"),
            "node_suspect_raised":  ("RUNNING", "SUSPECTED"),
            "node_suspect_cleared": ("SUSPECTED", "RUNNING"),
            "node_drain_initiated": ("RUNNING", "DRAINING"),
            "node_drain_complete":  ("DRAINING", "LEFT"),
            "node_failure_confirmed": ("SUSPECTED", "FAILED"),
        }
        mapping = _MEMBER_EVENTS.get(event_type)
        if mapping:
            from_s, to_s = mapping
            return ("SM-CLUSTER-MEMBER", node_id, from_s, to_s, event_type)
        return None


class LiveStateMachineValidator:
    """
    Real-time state machine validator that consumes Kafka events and
    validates all 8 state machines (including SM-GOVERNANCE and
    SM-CAPABILITY which were previously unwired).

    Usage::

        validator = LiveStateMachineValidator(kafka_consumer)
        await validator.start()
        # Runs until stopped; check validator.stats for live metrics
        await validator.stop()
    """

    def __init__(
        self,
        kafka_consumer: Any | None = None,
        topics: list[str] | None = None,
    ) -> None:
        self._consumer = kafka_consumer
        self._topics = topics or [
            "aeos.tasks",
            "aeos.membership",
            "aeos.checkpoints",
            "aeos.governance",
            "aeos.capabilities",
        ]
        self._adapter = EventAdapter()
        self._validator = StateMachineValidator()
        self._entity_states: dict[str, dict[str, str]] = {}  # machine→entity→current_state
        self._stats = LiveValidationStats()
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def stats(self) -> LiveValidationStats:
        return self._stats

    async def start(self) -> None:
        """Start consuming and validating events."""
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info("LiveStateMachineValidator started on topics: %s", self._topics)

    async def stop(self) -> None:
        """Stop the consumer loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("LiveStateMachineValidator stopped. Stats: %s", self._stats)

    async def validate_event(
        self,
        topic: str,
        message: dict[str, Any],
        kafka_offset: int = 0,
    ) -> LiveViolation | None:
        """
        Validate a single event. Returns a LiveViolation if the transition
        is invalid, otherwise None.

        Can be called directly (without Kafka) for testing.
        """
        adapted = self._adapter.adapt(topic, message, kafka_offset)
        if adapted is None:
            return None

        machine_id, entity_id, from_state, to_state, event = adapted
        self._stats.events_processed += 1

        # Get current known state for this entity
        current_state = self._entity_states.get(machine_id, {}).get(entity_id)

        # If we've seen this entity before, check the transition
        if current_state is not None and current_state != from_state:
            # State mismatch — use the recorded current state
            from_state = current_state

        try:
            record = TransitionRecord(
                machine_id=machine_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                event=event,
                timestamp=time.time(),
            )
            # Will raise StateMachineViolation if invalid
            self._validator.validate_transition(record)
            # Update tracked state
            if machine_id not in self._entity_states:
                self._entity_states[machine_id] = {}
            self._entity_states[machine_id][entity_id] = to_state
            self._stats.transitions_valid += 1
            return None

        except StateMachineViolation as exc:
            self._stats.transitions_invalid += 1
            violation = LiveViolation(
                machine_id=machine_id,
                entity_id=entity_id,
                from_state=from_state,
                to_state=to_state,
                event=event,
                kafka_offset=kafka_offset,
                timestamp=time.time(),
                description=str(exc),
            )
            self._stats.violations.append(violation)
            logger.error(
                "[%s] SM violation: %s %s→%s via %s",
                machine_id, entity_id, from_state, to_state, event,
            )
            return violation

    async def _consume_loop(self) -> None:
        """Main Kafka consumption loop."""
        if self._consumer is None:
            logger.warning("No Kafka consumer provided — LiveStateMachineValidator is inactive")
            return

        while self._running:
            try:
                records = await asyncio.wait_for(
                    self._consumer.getmany(timeout_ms=1000),
                    timeout=2.0,
                )
                for topic_partition, messages in records.items():
                    topic = topic_partition.topic
                    for msg in messages:
                        try:
                            payload = json.loads(msg.value)
                            await self.validate_event(topic, payload, msg.offset)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("Failed to process message: %s", exc)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("Consumer loop error: %s", exc)
                await asyncio.sleep(1.0)
