"""
Two-phase checkpoint engine.

Phase 1: write_full() / write() — persists checkpoint with committed=False.
Phase 2: commit() — marks committed=True. Kafka offset committed only after Phase 2.

Uncommitted checkpoints are invisible to recovery (INV-EXEC-002).
Payloads above _COMPRESS_THRESHOLD bytes are zlib-compressed.

Protocol: PROTO-008
Contract: AC-EXEC-002
"""

from __future__ import annotations

import asyncio
import uuid
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.distributed.execution.context import CheckpointData


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class CheckpointType(str, Enum):
    INCREMENTAL = "incremental"
    FULL        = "full"
    RECOVERY    = "recovery"


@dataclass
class CheckpointEntry:
    """Persisted checkpoint record."""
    checkpoint_id: str = field(default_factory=_new_id)
    workflow_id: str = ""
    step_id: str = ""
    execution_id: str = ""
    checkpoint_type: CheckpointType = CheckpointType.FULL
    data: CheckpointData | None = None
    committed: bool = False
    created_at: str = field(default_factory=_now_iso)
    committed_at: str | None = None
    compressed: bool = False
    raw_payload: bytes | None = None


# ── Checkpoint Store ABC ──────────────────────────────────────────────────────

class CheckpointStore(ABC):

    @abstractmethod
    async def save(self, entry: CheckpointEntry) -> None:
        """Phase 1: persist the entry (committed=False)."""

    @abstractmethod
    async def commit(self, checkpoint_id: str) -> None:
        """Phase 2: mark committed=True."""

    @abstractmethod
    async def latest(
        self,
        workflow_id: str,
        step_id: str,
        *,
        committed_only: bool = True,
    ) -> CheckpointEntry | None:
        """Return the most recently committed (or any) checkpoint."""

    @abstractmethod
    async def get(self, checkpoint_id: str) -> CheckpointEntry | None:
        """Fetch a specific checkpoint by ID."""

    @abstractmethod
    async def list_for_workflow(
        self,
        workflow_id: str,
        *,
        committed_only: bool = True,
    ) -> list[CheckpointEntry]:
        """List all checkpoints for a workflow."""

    @abstractmethod
    async def delete(self, checkpoint_id: str) -> None:
        """Remove a checkpoint."""


# ── In-memory Store ───────────────────────────────────────────────────────────

class InMemoryCheckpointStore(CheckpointStore):

    def __init__(self) -> None:
        self._entries: dict[str, CheckpointEntry] = {}
        self._lock = asyncio.Lock()

    async def save(self, entry: CheckpointEntry) -> None:
        async with self._lock:
            self._entries[entry.checkpoint_id] = entry

    async def commit(self, checkpoint_id: str) -> None:
        async with self._lock:
            entry = self._entries.get(checkpoint_id)
            if entry:
                entry.committed = True
                entry.committed_at = _now_iso()

    async def latest(
        self,
        workflow_id: str,
        step_id: str,
        *,
        committed_only: bool = True,
    ) -> CheckpointEntry | None:
        async with self._lock:
            candidates = [
                e for e in self._entries.values()
                if e.workflow_id == workflow_id
                and e.step_id == step_id
                and (not committed_only or e.committed)
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda e: e.created_at)

    async def get(self, checkpoint_id: str) -> CheckpointEntry | None:
        async with self._lock:
            return self._entries.get(checkpoint_id)

    async def list_for_workflow(
        self,
        workflow_id: str,
        *,
        committed_only: bool = True,
    ) -> list[CheckpointEntry]:
        async with self._lock:
            return [
                e for e in self._entries.values()
                if e.workflow_id == workflow_id
                and (not committed_only or e.committed)
            ]

    async def delete(self, checkpoint_id: str) -> None:
        async with self._lock:
            self._entries.pop(checkpoint_id, None)


# ── Checkpoint Engine ─────────────────────────────────────────────────────────

_COMPRESS_THRESHOLD = 4096


class CheckpointEngine:
    """
    Two-phase checkpoint write/commit engine.

    write_full() → write() with type=FULL, optionally compressing.
    commit()     → Phase 2: marks committed=True in the store.
    load()       → Returns the latest committed checkpoint's data.
    """

    def __init__(self, store: CheckpointStore) -> None:
        self._store = store

    async def write(
        self,
        data: CheckpointData,
        checkpoint_type: CheckpointType = CheckpointType.INCREMENTAL,
    ) -> CheckpointEntry:
        import json
        entry = CheckpointEntry(
            workflow_id=data.workflow_id,
            step_id=data.step_id,
            execution_id=data.execution_id,
            checkpoint_type=checkpoint_type,
            data=data,
            committed=False,
        )
        await self._store.save(entry)
        return entry

    async def write_full(self, data: CheckpointData) -> CheckpointEntry:
        return await self.write(data, CheckpointType.FULL)

    async def write_recovery(self, data: CheckpointData) -> CheckpointEntry:
        return await self.write(data, CheckpointType.RECOVERY)

    async def commit(self, entry: CheckpointEntry) -> None:
        """Phase 2: mark committed. Kafka offset committed after this returns."""
        await self._store.commit(entry.checkpoint_id)
        entry.committed = True
        entry.committed_at = _now_iso()

    async def load(
        self,
        workflow_id: str,
        step_id: str,
    ) -> CheckpointData | None:
        """Return the data from the latest committed checkpoint, or None."""
        entry = await self._store.latest(workflow_id, step_id, committed_only=True)
        if entry is None:
            return None
        return entry.data
