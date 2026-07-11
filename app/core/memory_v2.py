"""
AEOS Memory System v2 — 4-Tier Architecture

Replaces the v1 two-tier memory with a structured 4-tier system.
See docs/architecture/009-MEMORY_SYSTEM.md for full specification.

Tiers:
  Tier 1 — Sensory Buffer    (per-request, <1s TTL, 64KB max)
  Tier 2 — Working Memory    (per-task, cleared on task end, 100 entries)
  Tier 3 — Long-Term Semantic Memory (process lifetime, LRU eviction, 1000 entries)
  Tier 4 — Episodic Memory   (session lifetime, append-only, future phase)

Design:
  - All writes are explicit (no automatic capture)
  - Each tier has defined eviction policy
  - All access emits a counter metric
  - Tier 2 and 3 are searchable (substring key search)

Backward compatible with v1 MemoryStore API:
  write_long(), write_short(), get_task_context(), clear_task(), summarize()
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.core.logger import get_logger

__all__ = [
    "MemoryEntry",
    "SensoryBuffer",
    "WorkingMemory",
    "LongTermMemory",
    "EpisodicMemory",
    "MemorySystemV2",
    "get_memory_v2",
]

log = get_logger(__name__)

_TIER2_MAX_ENTRIES = 100
_TIER3_MAX_ENTRIES = 1_000
_SENSORY_TTL_SECONDS = 1.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Entry Schema ───────────────────────────────────────────────────────────────

@dataclass
class MemoryEntry:
    key: str
    value: Any
    tier: int
    agent_id: str = ""
    task_id: str = ""
    written_at: str = field(default_factory=_now)
    expires_at: float = 0.0     # 0 = never

    @property
    def is_expired(self) -> bool:
        return self.expires_at > 0 and time.time() > self.expires_at


# ── Tier 1: Sensory Buffer ────────────────────────────────────────────────────

class SensoryBuffer:
    """
    Tier 1: Single-request raw input buffer.

    Stores the raw task string and request headers before any parsing.
    TTL: < 1 second. Cleared after Stage 3 consumes it.
    Max size: 64 KB of key-value pairs.
    """

    _MAX_BYTES = 64 * 1024

    def __init__(self) -> None:
        self._store: dict[str, MemoryEntry] = {}
        self._writes = 0
        self._reads = 0

    def write(self, key: str, value: Any, request_id: str = "") -> None:
        entry = MemoryEntry(
            key=key, value=value, tier=1,
            task_id=request_id,
            expires_at=time.time() + _SENSORY_TTL_SECONDS,
        )
        self._store[key] = entry
        self._writes += 1

    def read(self, key: str) -> Optional[Any]:
        self._reads += 1
        entry = self._store.get(key)
        if entry is None or entry.is_expired:
            return None
        return entry.value

    def clear(self) -> None:
        self._store.clear()

    def flush_expired(self) -> int:
        expired = [k for k, e in self._store.items() if e.is_expired]
        for k in expired:
            del self._store[k]
        return len(expired)

    def summarize(self) -> dict:
        return {"tier": 1, "entries": len(self._store), "writes": self._writes, "reads": self._reads}


# ── Tier 2: Working Memory ────────────────────────────────────────────────────

class WorkingMemory:
    """
    Tier 2: Short-term working memory scoped to a task's lifetime.

    Owner: Execution Engine Step Executor.
    Cleared via clear_task() at end of each task (Stage 15).
    Max: 100 entries per task.
    """

    def __init__(self) -> None:
        # task_id → {key → MemoryEntry}
        self._store: dict[str, dict[str, MemoryEntry]] = {}
        self._writes = 0
        self._reads = 0

    def write(self, task_id: str, key: str, value: Any, agent_id: str = "") -> None:
        task_store = self._store.setdefault(task_id, {})

        # Evict oldest entry if at limit
        if len(task_store) >= _TIER2_MAX_ENTRIES and key not in task_store:
            oldest_key = next(iter(task_store))
            del task_store[oldest_key]
            log.debug("Tier2 eviction", extra={"ctx_task_id": task_id, "ctx_evicted_key": oldest_key})

        task_store[key] = MemoryEntry(key=key, value=value, tier=2, agent_id=agent_id, task_id=task_id)
        self._writes += 1

    def read(self, task_id: str, key: str) -> Optional[Any]:
        self._reads += 1
        entry = self._store.get(task_id, {}).get(key)
        return entry.value if entry else None

    def get_all(self, task_id: str) -> dict[str, Any]:
        return {k: e.value for k, e in self._store.get(task_id, {}).items()}

    def clear_task(self, task_id: str) -> int:
        task_store = self._store.pop(task_id, {})
        return len(task_store)

    def search(self, task_id: str, query: str) -> list[dict[str, Any]]:
        """Substring key search within a task's working memory."""
        results = []
        for k, e in self._store.get(task_id, {}).items():
            if query.lower() in k.lower():
                results.append({"key": k, "value": e.value})
        return results

    def summarize(self) -> dict:
        return {
            "tier": 2,
            "active_tasks": len(self._store),
            "total_entries": sum(len(v) for v in self._store.values()),
            "writes": self._writes,
            "reads": self._reads,
        }


# ── Tier 3: Long-Term Semantic Memory ─────────────────────────────────────────

class LongTermMemory:
    """
    Tier 3: Process-lifetime semantic memory shared across all agents.

    Owner: All agents, Execution Engine.
    Eviction: LRU (Least Recently Used) when at capacity.
    Capacity: 1000 entries.
    """

    def __init__(self) -> None:
        # OrderedDict maintains LRU order
        self._store: OrderedDict[str, MemoryEntry] = OrderedDict()
        self._writes = 0
        self._reads = 0

    def write(self, key: str, value: Any, agent_id: str = "", task_id: str = "") -> None:
        # LRU eviction
        if len(self._store) >= _TIER3_MAX_ENTRIES and key not in self._store:
            evicted_key, _ = self._store.popitem(last=False)
            log.debug("Tier3 LRU eviction", extra={"ctx_evicted_key": evicted_key})

        entry = MemoryEntry(key=key, value=value, tier=3, agent_id=agent_id, task_id=task_id)
        if key in self._store:
            del self._store[key]  # Remove then re-insert to update LRU position
        self._store[key] = entry
        self._writes += 1

    def read(self, key: str) -> Optional[Any]:
        self._reads += 1
        entry = self._store.get(key)
        if entry is None:
            return None
        # Move to end (most recently used)
        self._store.move_to_end(key)
        return entry.value

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """
        Substring key/value search. Returns top_k matches.
        Future: embedding-based semantic search.
        """
        results = []
        q = query.lower()
        for key, entry in reversed(list(self._store.items())):
            if q in key.lower() or (isinstance(entry.value, str) and q in entry.value.lower()):
                results.append({
                    "key": key,
                    "value": entry.value,
                    "agent_id": entry.agent_id,
                    "written_at": entry.written_at,
                })
                if len(results) >= top_k:
                    break
        return results

    def get_recent(self, n: int = 10) -> list[dict[str, Any]]:
        """Return the N most recently written entries."""
        items = list(self._store.items())[-n:]
        return [{"key": k, "value": e.value, "written_at": e.written_at} for k, e in reversed(items)]

    def summarize(self) -> dict:
        return {
            "tier": 3,
            "entries": len(self._store),
            "capacity": _TIER3_MAX_ENTRIES,
            "writes": self._writes,
            "reads": self._reads,
        }


# ── Tier 4: Episodic Memory (stub — future phase) ─────────────────────────────

class EpisodicMemory:
    """
    Tier 4: Session-lifetime episodic memory.

    Stores a chronological sequence of task episodes for session continuity.
    Full implementation is a future phase (requires session manager integration).
    """

    def __init__(self) -> None:
        self._episodes: list[dict[str, Any]] = []
        self._writes = 0

    def append(self, episode: dict[str, Any]) -> None:
        self._episodes.append({**episode, "appended_at": _now()})
        self._writes += 1

    def get_recent(self, n: int = 5) -> list[dict[str, Any]]:
        return self._episodes[-n:]

    def search(self, query: str) -> list[dict[str, Any]]:
        q = query.lower()
        return [e for e in self._episodes if q in str(e).lower()]

    def summarize(self) -> dict:
        return {"tier": 4, "episodes": len(self._episodes), "writes": self._writes}


# ── MemorySystemV2 ─────────────────────────────────────────────────────────────

class MemorySystemV2:
    """
    4-tier memory system.

    Implements the v1 MemoryStore interface for backward compatibility
    plus the v2 tier-specific API.
    """

    def __init__(self) -> None:
        self.sensory   = SensoryBuffer()
        self.working   = WorkingMemory()
        self.long_term = LongTermMemory()
        self.episodic  = EpisodicMemory()

    # ── v1 backward-compatible API ─────────────────────────────────────────────

    def write_short(self, task_id: str, key: str, value: Any, agent_id: str = "") -> None:
        """Write to Tier 2 (Working Memory). v1 compatible."""
        self.working.write(task_id, key, value, agent_id=agent_id)

    def write_long(self, key: str, value: Any, agent_id: str = "", task_id: str = "") -> None:
        """Write to Tier 3 (Long-Term Memory). v1 compatible."""
        self.long_term.write(key, value, agent_id=agent_id, task_id=task_id)

    def get_task_context(self, task_id: str) -> dict[str, Any]:
        """Return all Tier 2 entries for a task. v1 compatible."""
        return self.working.get_all(task_id)

    def clear_task(self, task_id: str) -> None:
        """Clear Tier 2 for a completed task. v1 compatible."""
        cleared = self.working.clear_task(task_id)
        log.debug("Working memory cleared", extra={"ctx_task_id": task_id, "ctx_cleared": cleared})

    def search_long_term(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        """Search Tier 3 by keyword."""
        return self.long_term.search(query, top_k=top_k)

    # ── v2 tier-direct API ─────────────────────────────────────────────────────

    def write_sensory(self, key: str, value: Any, request_id: str = "") -> None:
        self.sensory.write(key, value, request_id=request_id)

    def read_sensory(self, key: str) -> Optional[Any]:
        return self.sensory.read(key)

    def append_episode(self, episode: dict[str, Any]) -> None:
        self.episodic.append(episode)

    # ── Introspection ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        return {
            "v2": True,
            "tiers": {
                "tier1_sensory":    self.sensory.summarize(),
                "tier2_working":    self.working.summarize(),
                "tier3_long_term":  self.long_term.summarize(),
                "tier4_episodic":   self.episodic.summarize(),
            }
        }


# ── Singleton ──────────────────────────────────────────────────────────────────

_memory_v2: Optional[MemorySystemV2] = None


def get_memory_v2() -> MemorySystemV2:
    global _memory_v2
    if _memory_v2 is None:
        _memory_v2 = MemorySystemV2()
    return _memory_v2
