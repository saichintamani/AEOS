"""
AEOS — Shared Agent Memory
Two-tier memory accessible by all agents through the orchestrator:

  Short-term  — task-scoped, lives only for the duration of one run_task() call.
                Cleared automatically when the task ends.

  Long-term   — cross-task, survives the lifetime of the process.
                LRU-evicted when capacity is exceeded.
                Searchable via keyword overlap scoring (no RAG needed here).

Usage:
    from app.core.memory import get_memory
    mem = get_memory()
    mem.write_short(task_id, "key", value, agent_id="my_agent")
    mem.write_long("important_fact", value)
    results = mem.search_long("neural network")
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger(__name__)


@dataclass
class MemoryEntry:
    key: str
    value: Any
    timestamp: str
    task_id: str
    agent_id: str
    memory_type: str   # "short" | "long"


class AgentMemory:
    """
    In-process shared memory for all agents.

    Short-term:  dict[task_id → dict[key → MemoryEntry]]
    Long-term:   list[MemoryEntry]  (ordered insertion, LRU-evicted at capacity)
    """

    def __init__(self) -> None:
        self._short: dict[str, dict[str, MemoryEntry]] = {}
        self._long: list[MemoryEntry] = []
        self._capacity: int = settings.memory_max_long_term
        log.info("AgentMemory initialized", extra={"ctx_capacity": self._capacity})

    # ── Timestamp helper ───────────────────────────────────────────────────────

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    # ── Short-term ─────────────────────────────────────────────────────────────

    def write_short(
        self,
        task_id: str,
        key: str,
        value: Any,
        agent_id: str = "",
    ) -> None:
        """Write a value into short-term memory for a specific task."""
        if task_id not in self._short:
            self._short[task_id] = {}
        self._short[task_id][key] = MemoryEntry(
            key=key,
            value=value,
            timestamp=self._now(),
            task_id=task_id,
            agent_id=agent_id,
            memory_type="short",
        )
        log.debug(
            "Short-term write",
            extra={"ctx_task_id": task_id, "ctx_key": key, "ctx_agent": agent_id},
        )

    def read_short(self, task_id: str, key: str) -> Any | None:
        """Read a value from short-term memory. Returns None if not found."""
        entry = self._short.get(task_id, {}).get(key)
        return entry.value if entry else None

    def get_task_context(self, task_id: str) -> dict:
        """
        Return the full short-term memory snapshot for a task.
        Format: {key: value, ...} — values only, stripped of MemoryEntry wrapper.
        """
        task_mem = self._short.get(task_id, {})
        return {k: e.value for k, e in task_mem.items()}

    def clear_task(self, task_id: str) -> None:
        """Remove all short-term memory for a completed task."""
        count = len(self._short.pop(task_id, {}))
        if count:
            log.debug(
                "Short-term cleared",
                extra={"ctx_task_id": task_id, "ctx_entries_removed": count},
            )

    # ── Long-term ──────────────────────────────────────────────────────────────

    def write_long(
        self,
        key: str,
        value: Any,
        agent_id: str = "",
        task_id: str = "",
    ) -> None:
        """
        Write to long-term memory. If the key already exists, it is updated
        (old entry removed, new one appended). Evicts oldest entry if over capacity.
        """
        # Remove existing entry with same key (dedup by key)
        self._long = [e for e in self._long if e.key != key]

        entry = MemoryEntry(
            key=key,
            value=value,
            timestamp=self._now(),
            task_id=task_id,
            agent_id=agent_id,
            memory_type="long",
        )
        self._long.append(entry)

        # LRU eviction
        if len(self._long) > self._capacity:
            evicted = self._long.pop(0)
            log.debug("Long-term evicted", extra={"ctx_evicted_key": evicted.key})

        log.debug(
            "Long-term write",
            extra={"ctx_key": key, "ctx_agent": agent_id, "ctx_total": len(self._long)},
        )

    def read_long(self, key: str) -> Any | None:
        """Exact-key lookup in long-term memory. Returns None if not found."""
        for entry in reversed(self._long):
            if entry.key == key:
                return entry.value
        return None

    def search_long(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        """
        Keyword overlap search over long-term memory keys and string values.
        Scores each entry by the fraction of query words that appear in
        the entry's key + value string representation.

        Returns up to top_k entries, ranked by descending score.
        """
        if not self._long:
            return []

        query_words = set(re.findall(r"\w+", query.lower()))
        if not query_words:
            return []

        scored: list[tuple[float, MemoryEntry]] = []
        for entry in self._long:
            entry_text = f"{entry.key} {entry.value!s}".lower()
            entry_words = set(re.findall(r"\w+", entry_text))
            overlap = len(query_words & entry_words) / len(query_words)
            if overlap > 0:
                scored.append((overlap, entry))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def summarize(self) -> dict:
        """Return a metadata snapshot of the current memory state."""
        agents_seen = list({e.agent_id for e in self._long if e.agent_id})
        recent_keys = [e.key for e in self._long[-10:]]
        short_task_count = len(self._short)
        return {
            "long_term_count": len(self._long),
            "long_term_capacity": self._capacity,
            "short_term_active_tasks": short_task_count,
            "recent_long_term_keys": recent_keys,
            "agents_seen": agents_seen,
        }


@lru_cache(maxsize=1)
def get_memory() -> AgentMemory:
    """Cached singleton. All agents and the orchestrator share one instance."""
    return AgentMemory()
