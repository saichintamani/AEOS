"""
FileCheckpointStore — a durable, on-disk CheckpointStore for the cross-process
recovery testbed.

This is TEST SCAFFOLDING, not a product store: it exists so the *existing*
CheckpointEngine (app/distributed/execution/checkpoint.py) and its two-phase
write→commit protocol can be exercised across a real OS-process restart without
requiring Redis. Production durability is provided by RedisCheckpointStore; this
adapter mirrors its committed/uncommitted semantics on the local filesystem so a
second process can read what a first process (now dead) durably committed.

It implements the real ``CheckpointStore`` ABC verbatim, so nothing about the
engine changes — the engine cannot tell it apart from the in-memory store except
that the data outlives the process.

Serialization: each CheckpointEntry is one JSON file named
``<workflow_id>__<step_id>__<checkpoint_id>.json``. The committed flag lives in
the file, so ``latest(committed_only=True)`` — the recovery path — only ever
returns durably-committed checkpoints (INV-EXEC-002).

Phase: 13 Sprint 3
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from app.distributed.execution.checkpoint import (
    CheckpointEntry,
    CheckpointStore,
    CheckpointType,
)
from app.distributed.execution.context import CheckpointData
from app.distributed.execution.states import ExecutionState


def _encode(obj):
    # ExecutionState / CheckpointType are str-enums; bytes payloads are not used
    # by the testbed workflow (payload stays None).
    if isinstance(obj, bytes):
        return obj.decode("latin-1")
    return getattr(obj, "value", str(obj))


class FileCheckpointStore(CheckpointStore):
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    # ── write path ────────────────────────────────────────────────────────────
    async def save(self, entry: CheckpointEntry) -> None:
        self._write(entry)

    async def commit(self, checkpoint_id: str) -> None:
        for path in self._root.glob(f"*__{checkpoint_id}.json"):
            entry = self._read(path)
            if entry is not None:
                entry.committed = True
                from app.distributed.execution.checkpoint import _now_iso
                entry.committed_at = _now_iso()
                self._write(entry)

    # ── read path ─────────────────────────────────────────────────────────────
    async def latest(
        self, workflow_id: str, step_id: str, *, committed_only: bool = True,
    ) -> CheckpointEntry | None:
        candidates = [
            e for e in self._all()
            if e.workflow_id == workflow_id and e.step_id == step_id
            and (not committed_only or e.committed)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e.created_at)

    async def latest_for_workflow(
        self, workflow_id: str, *, committed_only: bool = True,
    ) -> CheckpointEntry | None:
        """Convenience for the testbed: newest committed checkpoint across all
        steps of a workflow (ordered by the checkpoint's step_index)."""
        candidates = [
            e for e in self._all()
            if e.workflow_id == workflow_id
            and (not committed_only or e.committed)
            and e.data is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda e: (e.data.step_index, e.created_at))

    async def get(self, checkpoint_id: str) -> CheckpointEntry | None:
        for path in self._root.glob(f"*__{checkpoint_id}.json"):
            return self._read(path)
        return None

    async def list_for_workflow(
        self, workflow_id: str, *, committed_only: bool = True,
    ) -> list[CheckpointEntry]:
        return [
            e for e in self._all()
            if e.workflow_id == workflow_id
            and (not committed_only or e.committed)
        ]

    async def delete(self, checkpoint_id: str) -> None:
        for path in self._root.glob(f"*__{checkpoint_id}.json"):
            path.unlink(missing_ok=True)

    # ── serialization ─────────────────────────────────────────────────────────
    def _path(self, entry: CheckpointEntry) -> Path:
        safe_wf = entry.workflow_id or "_"
        safe_step = entry.step_id or "_"
        return self._root / f"{safe_wf}__{safe_step}__{entry.checkpoint_id}.json"

    def _write(self, entry: CheckpointEntry) -> None:
        doc = {
            "checkpoint_id": entry.checkpoint_id,
            "workflow_id": entry.workflow_id,
            "step_id": entry.step_id,
            "execution_id": entry.execution_id,
            "checkpoint_type": entry.checkpoint_type.value,
            "committed": entry.committed,
            "created_at": entry.created_at,
            "committed_at": entry.committed_at,
            "data": dataclasses.asdict(entry.data) if entry.data else None,
        }
        blob = json.dumps(doc, default=_encode).encode()
        path = self._path(entry)
        # Atomic-ish write: temp then replace, so a reader never sees a partial file.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)

    def _read(self, path: Path) -> CheckpointEntry | None:
        try:
            doc = json.loads(path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return None
        data = None
        if doc.get("data"):
            d = dict(doc["data"])
            d["state"] = ExecutionState(d["state"]) if d.get("state") else ExecutionState.RUNNING
            d["payload"] = None
            data = CheckpointData(**d)
        return CheckpointEntry(
            checkpoint_id=doc["checkpoint_id"],
            workflow_id=doc["workflow_id"],
            step_id=doc["step_id"],
            execution_id=doc.get("execution_id", ""),
            checkpoint_type=CheckpointType(doc.get("checkpoint_type", "full")),
            data=data,
            committed=doc.get("committed", False),
            created_at=doc.get("created_at", ""),
            committed_at=doc.get("committed_at"),
        )

    def _all(self) -> list[CheckpointEntry]:
        out = []
        for path in self._root.glob("*.json"):
            entry = self._read(path)
            if entry is not None:
                out.append(entry)
        return out
