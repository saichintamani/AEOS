# 009 — AEOS Memory System

| Field       | Value                                         |
|-------------|-----------------------------------------------|
| **Status**  | Approved                                      |
| **Version** | 1.0.0                                         |
| **Date**    | 2026-07-05                                    |
| **Authors** | AEOS Platform Team                            |
| **Extends** | `app/core/memory.py` (v1 two-tier memory)     |

---

## Abstract

The AEOS Memory System is the platform-wide information persistence and retrieval layer. It provides four distinct memory tiers, each optimized for a different scope, lifetime, and access pattern. This document formalizes the 4-tier model, specifies the complete API contract, defines isolation and consistency guarantees, describes eviction policies, and maps each tier to the agent cognitive steps that use it. The goal is to make memory a first-class, observable, and reliably-behaved system — not an implementation detail of individual agents.

---

## Table of Contents

1. [Motivation](#1-motivation)
2. [Memory as a First-Class Citizen](#2-memory-as-a-first-class-citizen)
3. [The 4-Tier Memory Model](#3-the-4-tier-memory-model)
4. [Memory Access API](#4-memory-access-api)
5. [Memory Entry Schema](#5-memory-entry-schema)
6. [Isolation Guarantees](#6-isolation-guarantees)
7. [Consistency Model](#7-consistency-model)
8. [Memory Serialization Format](#8-memory-serialization-format)
9. [Search Semantics](#9-search-semantics)
10. [Eviction Policies](#10-eviction-policies)
11. [How Agents Interact with Each Tier](#11-how-agents-interact-with-each-tier)
12. [Future: Distributed Memory](#12-future-distributed-memory)
13. [Observability](#13-observability)
14. [Memory Anti-Patterns](#14-memory-anti-patterns)
15. [Cross-References](#15-cross-references)

---

## 1. Motivation

### 1.1 The Limits of Stateless Agent Execution

The simplest model of an AI agent is a stateless function: take an input, call an LLM, return an output. This model works for isolated, one-shot tasks. It breaks down the moment agents need to:

- Reference the result of a previous step in the same pipeline
- Avoid repeating research that was already done earlier in the session
- Learn from past task patterns to make better decisions
- Maintain a coherent narrative across multiple user interactions in a session
- Share findings between concurrently-running agents that are solving different parts of the same problem

Without structured memory, every one of these requirements is handled ad-hoc: passing enormous context strings into every LLM call, re-running expensive searches that were already completed, or simply ignoring past results entirely. The outcome is bloated token costs, inconsistent behavior, and agents that cannot grow more capable with experience.

### 1.2 Why Structure Matters

Unstructured memory — a single dict or a blob of text — does not scale. As the number of concurrent tasks grows, key collisions occur. As history accumulates, search becomes linear. As the system evolves, there is no way to reason about what memory contains, how old it is, or who wrote it.

AEOS v2 formalizes memory as a structured, multi-tier system with:
- **Typed entries:** every piece of memory has a schema, not just a raw value
- **Scoped lifetimes:** each tier has a defined scope and automatic cleanup
- **Observable access patterns:** every read and write emits a metric
- **Defined eviction:** no silent unbounded growth
- **Searchable:** meaningful retrieval, not just key lookup

### 1.3 The Four Problems Four Tiers Solve

| Problem                                    | Tier That Solves It |
|--------------------------------------------|---------------------|
| Raw input must survive Stage 1 → Stage 3  | Tier 1: Sensory Buffer |
| Step N result must be available to Step N+1 within a task | Tier 2: Working Memory |
| Findings must be accessible across different tasks in the same process | Tier 3: Long-Term Semantic Memory |
| Related tasks in one user session must share a narrative thread | Tier 4: Episodic Memory |

---

## 2. Memory as a First-Class Citizen

### 2.1 The Philosophical Position

An autonomous system without persistent memory is not an autonomous system — it is a sophisticated calculator. Each call is independent; nothing is learned; no context is built. This is acceptable for a single-turn assistant but is fundamentally incompatible with the AEOS design goal: agents that grow more capable, context-aware, and efficient over time.

Memory is what transforms an agent from a stateless tool into a participant in an ongoing process. When an agent can recall that it researched a topic three tasks ago, cross-reference the current task's constraints with the outcome of a similar past task, or recognize that the current user session has already established certain facts — it exhibits genuine context-awareness.

This is not merely a technical optimization (fewer tokens, faster execution). It is a qualitative capability difference. Memory is what makes an agent's behavior coherent across time.

### 2.2 Design Principles

**Principle 1: Explicit over implicit.**  
Every piece of memory is written explicitly by an agent or the engine. There is no automatic "stuff everything in memory" behavior. The discipline of explicit writes forces agents and the engine to decide what is worth remembering.

**Principle 2: Scoped lifetime over global state.**  
Each tier has a defined lifetime. Memory that is no longer relevant is automatically evicted or cleared. The engine does not accumulate state indefinitely.

**Principle 3: Memory is observable.**  
Every memory operation emits a metric and, for significant operations, an event. Memory is not a black box; its behavior can be monitored, debugged, and optimized.

**Principle 4: Memory does not replace context.**  
Memory is not a replacement for well-structured agent prompts. It is a complement: agents use memory to retrieve relevant prior information, then incorporate it into their context. Agents that attempt to use memory as a substitute for clear task instructions will produce poor results.

**Principle 5: Separation of tiers.**  
Each tier has one owner and one access pattern. Tier 1 is owned by the Execution Engine's intake stages. Tier 2 is owned by the Step Executor. Tier 3 is shared by all agents and the engine. Tier 4 is owned by the Session Manager. Writing to the wrong tier is a design error, not just a performance issue.

---

## 3. The 4-Tier Memory Model

### Tier Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  TIER 1: Sensory Buffer                                         │
│  Scope: single request · TTL: <1 second · Storage: in-request  │
│  Size: 64KB max · Access: read-once · Owner: Engine Stage 1-3   │
└─────────────────────────────────────────────────────────────────┘
                              │ consumed by Stage 3, then cleared
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  TIER 2: Short-Term Working Memory                              │
│  Scope: task lifetime · TTL: task end · Storage: in-process dict│
│  Size: 100 entries/task · Access: read/write · Owner: Step Exec │
└─────────────────────────────────────────────────────────────────┘
                              │ promoted at task completion
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  TIER 3: Long-Term Semantic Memory                              │
│  Scope: process lifetime · TTL: LRU eviction · Storage: in-proc│
│  Capacity: 1000 entries · Access: all agents · Owner: All       │
└─────────────────────────────────────────────────────────────────┘
                              │ (future) promoted to Episodic
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│  TIER 4: Episodic Memory (future phase)                         │
│  Scope: session lifetime · Storage: append-only sequence        │
│  Access: Session Manager + agents · Search: chrono + semantic   │
└─────────────────────────────────────────────────────────────────┘
```

---

### 3.1 Tier 1: Sensory Buffer

**Status:** New in AEOS v2. Does not exist in v1.

**Scope:** Single request only. Data lives from the moment the request enters Stage 1 until Stage 3 consumes it.

**Purpose:**  
The Sensory Buffer holds the raw input before any parsing or transformation. This separation is important: it means the parsed form (in `ClassifiedIntent`) is always derived from a canonical raw input that can be referenced and re-read if parsing fails. It also provides a clean boundary: after Stage 3, no component should need the raw HTTP-level input.

The Sensory Buffer holds:
- The raw task string (before sanitization)
- HTTP request headers relevant to execution (content-type, caller-agent, request-id)
- Caller metadata extracted from the API key (tier, rate limits, feature flags)
- The timestamp of first receipt (for SLA measurement)

**Storage:** An in-request Python dict. No persistence. No cross-request sharing. The dict is created at Stage 1 entry and passed through to Stage 3 as part of the `RawIntent` object. After Stage 3 consumes it, it is set to `None` and garbage-collected.

**Access pattern:** Write once (Stage 1), read once (Stage 3), cleared immediately after read.

**Size limit:** 64KB across all keys combined. If raw input exceeds this, it is truncated and a warning is attached. (The 8KB input limit in Stage 2 ensures the task string alone is well within this.)

**Implementation:**
```python
@dataclass
class SensoryBuffer:
    raw_task: str
    raw_headers: dict[str, str]
    caller_metadata: dict[str, Any]
    received_at: str       # ISO 8601 UTC
    request_id: str
    _consumed: bool = False

    def consume(self) -> dict[str, Any]:
        """Read-once semantics. Returns contents and marks buffer consumed."""
        if self._consumed:
            raise MemoryError("SensoryBuffer already consumed")
        self._consumed = True
        return {
            "raw_task": self.raw_task,
            "raw_headers": self.raw_headers,
            "caller_metadata": self.caller_metadata,
            "received_at": self.received_at,
            "request_id": self.request_id,
        }
```

**Events emitted:** None. The Sensory Buffer is entirely internal to the engine intake.

---

### 3.2 Tier 2: Short-Term Working Memory

**Status:** Exists in AEOS v1 as `AgentMemory._short`. Formalized here.

**Scope:** Single task lifetime. Created when the task enters Stage 10 (Workflow Runtime Entry). Destroyed when Stage 15 (Governance Gate) completes the task.

**Purpose:**  
Working Memory is the shared scratchpad for all components participating in a single task execution. Its primary users are:
- The Step Executor (Stage 11): writes each node's result
- Agents: read upstream node results from their context
- The Aggregator (Stage 13): reads all node results to merge them
- The Reflection Gate (Stage 14): reads results to evaluate quality

Every key in Working Memory is namespaced by its source to prevent collisions:
- `step.{node_id}.result` — output of a specific execution graph node
- `step.{node_id}.status` — "completed" | "failed" | "timed_out" | "skipped"
- `agent.{agent_id}.thought` — the agent's reasoning trace for this task
- `plan.goals` — the serialized GoalSet for this task
- `plan.graph_id` — the ID of the compiled ExecutionGraph
- `workflow.revision_count` — number of revision iterations so far
- `workflow.started_at` — task start timestamp

**Storage:** `dict[task_id → dict[key → MemoryEntry]]`. This is the current v1 structure (`AgentMemory._short`), promoted to a formal contract.

**Access:** Any component participating in the task may read or write, provided it has the `task_id`. Cross-task access is not possible (keys are namespaced by `task_id`).

**Max entries per task:** Configurable via `settings.memory_max_short_term_per_task` (default: 100). If exceeded, the oldest entries for that task are evicted (LRU within the task scope). A warning is logged.

**Cleared:** Automatically at the end of Stage 15 via `mem.clear_task(task_id)`. This is a guaranteed cleanup — it happens in a `finally` block in the pipeline runner.

**Implementation (existing, with additions):**
```python
def write_short(
    self,
    task_id: str,
    key: str,
    value: Any,
    agent_id: str = "",
    ttl_seconds: Optional[float] = None,  # NEW in v2: optional per-entry TTL
) -> None: ...

def read_short(self, task_id: str, key: str) -> Any | None: ...

def get_task_context(self, task_id: str) -> dict: ...

def clear_task(self, task_id: str) -> None: ...

def list_task_keys(self, task_id: str) -> list[str]: ...  # NEW in v2

def get_task_entry(self, task_id: str, key: str) -> MemoryEntry | None: ...  # NEW in v2
```

**Events emitted:**
- `memory.short_term.written` (debug level)
- `memory.short_term.cleared` (info level, on task end)

---

### 3.3 Tier 3: Long-Term Semantic Memory

**Status:** Exists in AEOS v1 as `AgentMemory._long`. Formalized and extended here.

**Scope:** Process lifetime. Survives individual task completions; lost only when the AEOS process restarts (unless a persistence backend is added — see Section 12).

**Purpose:**  
Long-Term Memory stores knowledge that is valuable beyond a single task:
- Task summaries (what was the task, what was the result, which agents were used)
- Conclusions and key findings (facts extracted from research tasks)
- Learned patterns (this type of task works best with this sequence of agents)
- Cached computations (results of expensive searches that might be relevant to future tasks)

Long-Term Memory is shared across all tasks and all agents. Any component may read it. Writes are semantically meaningful: a write to Long-Term Memory is an assertion that this information is worth remembering for future tasks.

**Storage:** `list[MemoryEntry]` ordered by insertion time. The current v1 structure.

**Capacity:** Configurable via `settings.memory_max_long_term` (default: 1000 entries).

**Eviction:** LRU — when capacity is exceeded, the oldest entry (index 0 of the list) is removed. Updates to an existing key (same key written again) move the entry to the tail (most-recently-used position) and delete the old entry.

**Search:** Keyword overlap scoring (current v1 implementation). See Section 9 for full search semantics.

**Key conventions for Long-Term Memory:**
- `task_summary.{task_id}` — post-task summary written by Stage 15
- `task_reflection.{task_id}` — reflection output written by Stage 14
- `knowledge.{concept_name}` — a named fact or concept extracted by an agent
- `pattern.{task_type}.{pattern_name}` — a learned execution pattern
- `cache.{tool_id}.{query_hash}` — cached tool result (with TTL awareness)

**Access:** All agents, the Aggregator, the Reflection Gate, and the Governance Gate can read and write.

**Events emitted:**
- `memory.long_term.written`
- `memory.long_term.evicted` (when an entry is removed due to capacity)
- `memory.long_term.searched` (with query and result count)

---

### 3.4 Tier 4: Episodic Memory

**Status:** New in AEOS v2. Planned for a future phase. Specification provided here to guide architectural decisions.

**Scope:** Session lifetime. A "session" is a sequence of related tasks from the same caller within a time window (configurable; default: 2 hours of inactivity ends a session).

**Purpose:**  
Episodic Memory provides a narrative thread across multiple tasks. Where Long-Term Memory stores facts and summaries in isolation, Episodic Memory stores the story of what happened — in sequence, with causal links.

Use cases:
- "Remember what we did earlier in this session" — a user can ask AEOS to refer back to previous tasks in the current conversation
- Cross-task dependency tracking — Task B knows it follows Task A and can reference A's conclusions
- Session-level quality review — after a session, review all tasks completed and their outcomes
- Debugging — reconstruct exactly what happened across a multi-task workflow

**Structure of an Episode:**
```python
@dataclass
class Episode:
    episode_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    task_id: str = ""
    task_preview: str = ""              # first 200 chars of task
    task_type: str = ""                 # from ClassifiedIntent.task_type
    agents_used: list[str] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    outcome: str = ""                   # "completed" | "completed_partial" | "failed"
    quality_score: float = 0.0
    key_insights: list[str] = field(default_factory=list)  # extracted by Reflection Gate
    result_summary: str = ""            # first 500 chars of final result
    started_at: str = ""
    completed_at: str = ""
    duration_ms: float = 0.0
    cost_usd: float = 0.0
    sequence_number: int = 0            # order within the session
    related_episode_ids: list[str] = field(default_factory=list)  # linked episodes
```

**Storage:** Append-only sequence (`list[Episode]`) per session. Once written, episodes are never modified. (Corrections are new episodes that reference the corrected episode.)

**Search:**  
Two search strategies:
1. **Chronological:** Return the last N episodes in the session (most recent first).
2. **Semantic:** (future) Embedding similarity between query and episode `task_preview` + `key_insights`.

**Session management:**  
The Session Manager (a future AEOS service, not yet defined) owns Tier 4. It creates sessions, appends episodes, and expires old sessions.

**Events emitted:**
- `memory.episodic.episode_recorded`
- `memory.episodic.session_started`
- `memory.episodic.session_expired`

---

## 4. Memory Access API

The `AgentMemory` class provides the unified access interface for all tiers. The API is designed to be simple — it never blocks the caller (all operations are synchronous), and it never raises for normal not-found cases (returns `None`).

```python
from __future__ import annotations
from typing import Any, Optional
from app.core.memory import MemoryEntry, Episode


class AgentMemory:
    """
    Unified 4-tier memory interface.
    All methods are synchronous and thread-safe for read operations.
    Write operations are not thread-safe; the caller is responsible for
    task-level isolation (which the Execution Engine guarantees by design).
    """

    # ── Tier 1: Sensory Buffer ─────────────────────────────────────────────────

    def create_sensory_buffer(
        self,
        raw_task: str,
        raw_headers: dict[str, str],
        caller_metadata: dict[str, Any],
        received_at: str,
        request_id: str,
    ) -> SensoryBuffer:
        """Create a new Sensory Buffer for an incoming request."""
        ...

    # ── Tier 2: Short-Term Working Memory ─────────────────────────────────────

    def write_short(
        self,
        task_id: str,
        key: str,
        value: Any,
        agent_id: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> None:
        """
        Write a value into Short-Term Working Memory for a specific task.
        Overwrites existing value for the same key.
        Raises MemoryError if the task's entry count would exceed max_short_term_per_task.
        """
        ...

    def read_short(
        self,
        task_id: str,
        key: str,
    ) -> Any | None:
        """
        Read a value from Short-Term Working Memory.
        Returns None if the key does not exist or the task has no memory.
        Never raises.
        """
        ...

    def get_task_context(
        self,
        task_id: str,
    ) -> dict[str, Any]:
        """
        Return the full Short-Term Memory snapshot for a task.
        Format: {key: value} — unwrapped from MemoryEntry.
        Returns empty dict if the task has no memory.
        """
        ...

    def list_task_keys(
        self,
        task_id: str,
    ) -> list[str]:
        """Return all keys present in Short-Term Memory for a task."""
        ...

    def get_task_entry(
        self,
        task_id: str,
        key: str,
    ) -> MemoryEntry | None:
        """Return the full MemoryEntry (with metadata) for a key, or None."""
        ...

    def clear_task(
        self,
        task_id: str,
    ) -> int:
        """
        Remove all Short-Term Memory for a completed task.
        Returns the number of entries removed.
        Emits memory.short_term.cleared event.
        """
        ...

    # ── Tier 3: Long-Term Semantic Memory ─────────────────────────────────────

    def write_long(
        self,
        key: str,
        value: Any,
        agent_id: str = "",
        task_id: str = "",
        tags: list[str] | None = None,
    ) -> None:
        """
        Write to Long-Term Semantic Memory.
        If the key already exists, the old entry is removed and the new entry
        is appended (LRU promotion).
        Evicts the oldest entry if capacity is exceeded.
        Emits memory.long_term.written and (if eviction) memory.long_term.evicted.
        """
        ...

    def read_long(
        self,
        key: str,
    ) -> Any | None:
        """
        Exact-key lookup in Long-Term Semantic Memory.
        Returns None if not found. Does not affect LRU order.
        """
        ...

    def search_long(
        self,
        query: str,
        top_k: int = 5,
        tags: list[str] | None = None,
    ) -> list[MemoryEntry]:
        """
        Keyword overlap search over Long-Term Memory.
        Optional tag filter: only consider entries with at least one matching tag.
        Returns up to top_k entries ranked by descending overlap score.
        Emits memory.long_term.searched.
        """
        ...

    def delete_long(
        self,
        key: str,
    ) -> bool:
        """
        Remove a specific entry from Long-Term Memory by key.
        Returns True if found and removed, False if not found.
        """
        ...

    def evict_long(
        self,
        count: int = 1,
    ) -> list[str]:
        """
        Manually evict the N oldest Long-Term Memory entries.
        Returns the evicted keys. Useful for memory pressure management.
        """
        ...

    # ── Tier 4: Episodic Memory ────────────────────────────────────────────────

    def record_episode(
        self,
        session_id: str,
        episode: Episode,
    ) -> None:
        """
        Append an Episode to the session's episodic memory.
        Episode records are immutable once written.
        """
        ...

    def get_recent_episodes(
        self,
        session_id: str,
        n: int = 10,
    ) -> list[Episode]:
        """
        Return the N most recent episodes for a session, newest first.
        """
        ...

    def search_episodes(
        self,
        session_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[Episode]:
        """
        Search episodes for a session by semantic similarity to query.
        v2: keyword overlap. future: embedding similarity.
        """
        ...

    def expire_session(
        self,
        session_id: str,
    ) -> int:
        """
        Remove all episodic memory for a session.
        Returns the number of episodes removed.
        """
        ...

    # ── Observability ──────────────────────────────────────────────────────────

    def summarize(self) -> dict:
        """Return a snapshot of memory state across all tiers."""
        ...

    def get_metrics(self) -> MemoryMetrics:
        """Return accumulated operational metrics. See Section 13."""
        ...
```

---

## 5. Memory Entry Schema

The `MemoryEntry` is the atomic unit of memory storage across Tiers 2 and 3. It wraps the raw value with metadata needed for eviction, search, debugging, and observability.

### 5.1 Current v1 Schema

```python
@dataclass
class MemoryEntry:
    key: str
    value: Any
    timestamp: str       # ISO 8601 UTC
    task_id: str
    agent_id: str
    memory_type: str     # "short" | "long"
```

### 5.2 Extended v2 Schema

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
import uuid


@dataclass
class MemoryEntry:
    """
    v2 extended MemoryEntry. Backward-compatible with v1.
    All new fields have defaults so existing code that creates MemoryEntry
    without them continues to work.
    """
    # ── Core fields (exist in v1) ──────────────────────────────────────────────
    key: str
    value: Any
    timestamp: str               # ISO 8601 UTC — when this entry was written
    task_id: str
    agent_id: str
    memory_type: str             # "short" | "long" | "episodic"

    # ── v2 additions ──────────────────────────────────────────────────────────
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    schema_version: str = "2.0"

    # Tagging for filtered search
    tags: list[str] = field(default_factory=list)

    # Size tracking (approximate, in bytes)
    size_bytes: int = 0

    # TTL support for Tier 2 (None = no TTL = cleared with task)
    expires_at: Optional[str] = None  # ISO 8601 UTC

    # Provenance: which execution stage wrote this
    written_by_stage: int = 0    # Stage number (1-15); 0 = unknown

    # Access tracking (for LRU and observability)
    access_count: int = 0
    last_accessed_at: Optional[str] = None

    # Content type hint for deserialization
    value_type: str = "any"      # "str" | "dict" | "list" | "int" | "float" | "any"

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow().isoformat() + "Z" > self.expires_at

    def record_access(self) -> None:
        self.access_count += 1
        self.last_accessed_at = datetime.utcnow().isoformat() + "Z"

    def to_dict(self) -> dict:
        """JSON-safe serialization."""
        return {
            "entry_id": self.entry_id,
            "key": self.key,
            "value": self._serialize_value(),
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "memory_type": self.memory_type,
            "schema_version": self.schema_version,
            "tags": self.tags,
            "size_bytes": self.size_bytes,
            "expires_at": self.expires_at,
            "written_by_stage": self.written_by_stage,
            "access_count": self.access_count,
            "last_accessed_at": self.last_accessed_at,
            "value_type": self.value_type,
        }

    def _serialize_value(self) -> Any:
        """Ensure value is JSON-safe."""
        if isinstance(self.value, (str, int, float, bool, type(None))):
            return self.value
        if isinstance(self.value, (dict, list)):
            return self.value  # assume already JSON-safe
        return str(self.value)  # fallback: stringify

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryEntry":
        return cls(
            key=data["key"],
            value=data["value"],
            timestamp=data["timestamp"],
            task_id=data.get("task_id", ""),
            agent_id=data.get("agent_id", ""),
            memory_type=data.get("memory_type", "long"),
            entry_id=data.get("entry_id", str(uuid.uuid4())),
            schema_version=data.get("schema_version", "2.0"),
            tags=data.get("tags", []),
            size_bytes=data.get("size_bytes", 0),
            expires_at=data.get("expires_at"),
            written_by_stage=data.get("written_by_stage", 0),
            access_count=data.get("access_count", 0),
            last_accessed_at=data.get("last_accessed_at"),
            value_type=data.get("value_type", "any"),
        )
```

---

## 6. Isolation Guarantees

### 6.1 Tier 1 Isolation

Complete isolation. Each request creates its own `SensoryBuffer` instance. No sharing is possible by design (the buffer is an in-memory Python object tied to the request call stack).

### 6.2 Tier 2 Isolation

**Within a task:** All components processing the same `task_id` share access to the same Working Memory partition. This is intentional — it is how step results flow from one agent to the next.

**Between tasks:** No cross-task access. Tier 2 is partitioned by `task_id`. Component A processing task X cannot read the Working Memory of task Y. This is enforced by the API: every read/write call requires a `task_id`.

**Concurrent task isolation:** When multiple tasks run concurrently (parallel user requests), their Working Memory partitions are entirely independent. A bug in task X's agent that corrupts its Working Memory partition does not affect task Y.

**Implementation note:** The in-process `dict[task_id → dict[key → MemoryEntry]]` provides natural partitioning. In the future Redis backend (Section 12), keys are namespaced: `st:{task_id}:{key}`.

### 6.3 Tier 3 Sharing

Long-Term Memory is intentionally shared across all tasks and all agents. This is its primary value. The tradeoff is that a write from one task is immediately visible to all other tasks.

**Collision handling:** Keys in Long-Term Memory are not protected by task scope. If task A and task B both write to `knowledge.python_async`, the second writer wins (last-write-wins). This is acceptable because Long-Term Memory stores knowledge that is intended to be shared — the latest version of a fact is typically the most correct one.

**No cross-task locking:** There is no distributed lock. The `write_long` operation is `O(n)` (must scan the list to remove the existing entry with the same key). In the current in-process implementation, Python's GIL provides implicit serialization for list mutations within a single process. In the future Redis backend, atomic operations (`LREM` + `RPUSH`) replace this.

### 6.4 Tier 4 Isolation

Episodic Memory is partitioned by `session_id`. Different sessions do not share episodes. Within a session, all tasks and agents share the same episode history.

---

## 7. Consistency Model

### 7.1 Guarantee Level

AEOS Memory provides **sequential consistency within a single task** and **eventual consistency across tasks** for Tier 3.

- **Within a task (Tier 2):** All writes by an agent in step N are visible to the agent in step N+1 before step N+1 begins. This is guaranteed by the topological execution order in the Step Executor (Stage 11): nodes do not start until their dependencies have completed.

- **Across tasks (Tier 3):** A write to Long-Term Memory in task A becomes visible to task B at the point task B reads. There is no causal ordering guarantee between tasks. Task B may read a stale value if task A's write has not been committed yet. In the in-process implementation this is a non-issue (writes are synchronous). In a distributed backend, a replication lag of milliseconds is acceptable for the use cases Long-Term Memory serves.

### 7.2 Last-Write-Wins

When the same key is written twice (by the same or different agents), the later write wins. There is no merge logic, no conflict resolution, and no version vector. This is acceptable because:

1. Long-Term Memory keys follow the `{type}.{identifier}` naming convention. Collisions between logically distinct values are rare if key naming conventions are followed.
2. The most recent version of a fact is typically the most accurate. If an agent updates its conclusion about a topic, the new conclusion should supersede the old one.
3. Implementing conflict resolution (CRDTs, vector clocks) would add significant complexity for marginal benefit at the current scale.

### 7.3 Why This Is Acceptable

AEOS is not a financial system, a medical records system, or any domain where consistency failures have irreversible consequences. Memory consistency failures result in: an agent using a slightly stale fact. The result is a slightly suboptimal response — not a data loss incident. The Reflection Gate (Stage 14) provides a quality backstop that catches cases where stale data led to a poor result.

---

## 8. Memory Serialization Format

All memory entries that flow through the API (for logging, export, future persistence) are serialized to a versioned JSON format.

### 8.1 Serialization Rules

1. **Top-level schema version field:** Every serialized entry includes `"schema_version": "2.0"`. This allows future deserializers to handle old entries correctly.
2. **Value serialization:** Values must be JSON-safe. The `MemoryEntry._serialize_value()` method handles this:
   - Primitives (`str`, `int`, `float`, `bool`, `None`): pass through.
   - Dicts and lists: assumed JSON-safe (shallow check only; deep validation is the writer's responsibility).
   - All other types: `str(value)` — lossy but safe.
3. **Timestamp format:** All timestamps are ISO 8601 UTC: `2026-07-05T14:23:01.432Z`.
4. **Key constraints:** Keys must be printable ASCII, no whitespace, max 256 characters. The `write_short` and `write_long` methods validate this.

### 8.2 Example Serialized Entry

```json
{
  "entry_id": "f3a2b1c0-1234-5678-abcd-ef0123456789",
  "key": "task_summary.abc123",
  "value": {
    "task": "Research quantum computing breakthroughs in 2026",
    "result_summary": "Key breakthroughs include...",
    "agents_used": ["research_agent", "analyst_agent"],
    "quality_score": 0.87
  },
  "timestamp": "2026-07-05T14:23:01.432Z",
  "task_id": "abc123",
  "agent_id": "execution_engine",
  "memory_type": "long",
  "schema_version": "2.0",
  "tags": ["task_summary", "research"],
  "size_bytes": 312,
  "expires_at": null,
  "written_by_stage": 15,
  "access_count": 0,
  "last_accessed_at": null,
  "value_type": "dict"
}
```

---

## 9. Search Semantics

### 9.1 Tier 2 Search

Tier 2 (Working Memory) does not support search. Access is always by exact `task_id` + `key` pair. This is intentional: Working Memory is a structured scratchpad, not a knowledge base. All keys are known in advance (they follow the `step.{node_id}.result` convention).

### 9.2 Tier 3: Exact Lookup

```python
# O(n) reverse scan — finds most recent entry with this key
result = mem.read_long("knowledge.python_async")
```

Complexity: `O(n)` where `n` is the number of entries (at most 1000). Acceptable for current scale. The reverse scan (newest first) is intentional: it returns the most recently written value for a key, which is the "current truth."

### 9.3 Tier 3: Keyword Overlap Search (Current)

```python
results = mem.search_long("neural network training techniques", top_k=5)
```

**Algorithm (from v1, formalized):**
1. Tokenize the query into a set of lowercase words: `query_words = set(re.findall(r"\w+", query.lower()))`.
2. For each entry in Long-Term Memory, compute: `entry_text = f"{entry.key} {entry.value!s}".lower()` and `entry_words = set(re.findall(r"\w+", entry_text))`.
3. Score: `overlap = len(query_words & entry_words) / len(query_words)`.
4. Filter: only include entries with `overlap > 0`.
5. Sort descending by score.
6. Return top `top_k` entries.

**Complexity:** `O(n × m)` where `n` is the number of long-term entries and `m` is the average token count per entry. At 1000 entries with reasonable entry sizes, this is well within acceptable latency for the search use case.

**Limitations:**
- No stemming or lemmatization: "running" and "run" are different tokens.
- No IDF weighting: common words ("the", "and") are counted equally.
- No semantic similarity: "automobile" and "car" are unrelated by this algorithm.

### 9.4 Tier 3: Embedding Search (Future)

The keyword overlap algorithm will be replaced by embedding-based similarity search when:
- Long-Term Memory entries grow beyond 5000 entries (where keyword search quality degrades), OR
- AEOS gains a dedicated embedding service (a planned future component).

**Upgrade path:**
1. Each `MemoryEntry` gains an `embedding: list[float] | None = None` field (default `None`).
2. The `write_long` method calls the embedding service asynchronously after writing. If the service is unavailable, the entry is written without an embedding (falls back to keyword search).
3. The `search_long` method checks if embeddings are available. If yes, uses cosine similarity. If no, uses keyword overlap.
4. The switch is transparent to callers — the `search_long` API signature does not change.

### 9.5 Tier 4: Episodic Search

- **Chronological:** `get_recent_episodes(session_id, n=10)` — `O(n)` tail of the episode list.
- **Semantic:** (future) Embedding similarity between query and `Episode.task_preview + " " + " ".join(Episode.key_insights)`.

---

## 10. Eviction Policies

### 10.1 Tier 1: Request End

The `SensoryBuffer` is garbage-collected when the `RawIntent` object that holds it goes out of scope at the end of Stage 3. No explicit eviction needed. Lifetime: milliseconds.

### 10.2 Tier 2: Task Completion

Working Memory for a task is cleared explicitly by calling `mem.clear_task(task_id)` in the `finally` block of the pipeline runner at Stage 15. This is **guaranteed** to execute even if Stage 15 raises an exception.

```python
async def run_pipeline(task: str, ...) -> GovernanceGateResult:
    task_id = str(uuid.uuid4())
    try:
        result = await _execute_pipeline(task_id, task, ...)
        return result
    finally:
        mem.clear_task(task_id)  # always runs
```

**Per-entry TTL (v2 addition):** Entries written with `ttl_seconds` set will be treated as expired after that time. Expiry is checked on read: `read_short` returns `None` for expired entries and removes them lazily. A background cleanup task runs every 60 seconds to remove expired entries from all task partitions.

### 10.3 Tier 3: LRU Eviction

When `write_long` causes the entry count to exceed `settings.memory_max_long_term`:
1. Remove the entry at index 0 of `_long` (the oldest/least-recently-used entry).
2. Emit `memory.long_term.evicted` event with `{evicted_key, evicted_at, total_count_after}`.
3. Log at DEBUG level.

Updates to existing keys promote the entry to the tail (most-recently-used position), preventing frequently-updated entries from being evicted.

**Capacity tuning:**
- Default: 1000 entries.
- Each entry's average size target: 2KB. At 1000 entries: ~2MB of long-term memory.
- If entries average larger: reduce capacity accordingly to keep total memory under ~50MB.
- Configured via `settings.memory_max_long_term`.

### 10.4 Tier 4: Session Expiry

Sessions expire after `settings.session_inactivity_timeout` seconds (default: 7200 = 2 hours) of no new episodes being recorded. The `expire_session()` method removes all episodes for the session. Expiry is managed by the Session Manager (future service).

---

## 11. How Agents Interact with Each Tier

This section maps the 5 cognitive steps of the Agent Runtime (from 007-AGENT_RUNTIME.md: Observe, Orient, Decide, Act, Reflect) to their memory interactions.

### 11.1 Observe Step

The **Observe** step is the agent's first cognitive action: perceive the current state.

| Operation | Tier | Key Pattern | Purpose |
|-----------|------|-------------|---------|
| Read task context | Tier 2 | `plan.goals`, `step.{upstream_node_id}.result` | Understand what previous steps produced |
| Read long-term knowledge | Tier 3 | `knowledge.*`, `task_summary.*` | Check if relevant prior knowledge exists |
| Read session context (future) | Tier 4 | (via Session Manager) | Check session history for relevant prior tasks |

The agent reads but does not write during Observe. It is building a picture of current state.

### 11.2 Orient Step

The **Orient** step is the agent's synthesis: combine the observed state with its goal to form a situational understanding.

| Operation | Tier | Key Pattern | Purpose |
|-----------|------|-------------|---------|
| Search long-term memory | Tier 3 | `search_long(query)` | Find semantically related prior knowledge |
| Write reasoning trace | Tier 2 | `agent.{agent_id}.thought` | Record the agent's situational understanding for the Reflection Gate |

The agent searches Tier 3 for relevant background knowledge and writes its orientation summary to Tier 2 for observability.

### 11.3 Decide Step

The **Decide** step is the agent's planning: given the situation, decide what to do.

| Operation | Tier | Key Pattern | Purpose |
|-----------|------|-------------|---------|
| Read constraints | Tier 2 | `plan.goals` (goal deadline, resource budget) | Understand resource limits |
| Write decision trace | Tier 2 | `agent.{agent_id}.decision` | Record which tools/approach the agent chose |

### 11.4 Act Step

The **Act** step is the agent's execution: invoke tools, call LLMs, produce output.

| Operation | Tier | Key Pattern | Purpose |
|-----------|------|-------------|---------|
| Write step result | Tier 2 | `step.{node_id}.result` | Share output with downstream agents |
| Write step status | Tier 2 | `step.{node_id}.status` | Signal completion to the Step Executor |
| Write new knowledge | Tier 3 | `knowledge.{concept}` | Promote discovered facts to long-term memory |

The Act step is the primary writer. Writing to Tier 3 from the Act step is selective: only information with long-term value (key facts, research conclusions) should be promoted.

### 11.5 Reflect Step

The **Reflect** step is the agent's self-evaluation: assess the quality of its own output.

| Operation | Tier | Key Pattern | Purpose |
|-----------|------|-------------|---------|
| Read step result | Tier 2 | `step.{node_id}.result` | Read own output for self-assessment |
| Write reflection | Tier 2 | `agent.{agent_id}.reflection` | Record confidence score and quality assessment |
| Update knowledge | Tier 3 | `knowledge.{concept}` | Correct or amend a prior long-term entry if new evidence contradicts it |

The Reflect step's Tier 2 writes feed into the Reflection Gate (Stage 14).

### 11.6 Summary Table

| Cognitive Step | Tier 1 | Tier 2 (Read) | Tier 2 (Write) | Tier 3 (Read) | Tier 3 (Write) | Tier 4 |
|----------------|--------|--------------|----------------|--------------|----------------|--------|
| Observe        | —      | plan, upstream results | — | knowledge, summaries | — | episodes (future) |
| Orient         | —      | — | agent.thought | search_long | — | — |
| Decide         | —      | plan.goals | agent.decision | — | — | — |
| Act            | —      | upstream results | step.result, step.status | — | knowledge (selected) | — |
| Reflect        | —      | step.result | agent.reflection | — | knowledge (corrections) | — |

---

## 12. Future: Distributed Memory

The current in-process memory implementation is suitable for a single-node AEOS deployment. When AEOS scales to multiple nodes (horizontal scaling, high availability), the memory system must migrate to a distributed backend.

### 12.1 When to Upgrade

Upgrade triggers:
- AEOS is deployed on more than 1 node (load-balanced), OR
- Process restarts require memory to survive (persistent long-term memory), OR
- Long-term memory must be shared across AEOS instances serving different users.

### 12.2 Target Backend: Redis

Redis is the recommended upgrade target because:
- It supports all required data structures: strings (key-value), lists (ordered insertion + LRU), sorted sets (scored search results).
- It provides atomic operations for safe concurrent writes.
- It supports TTL natively.
- It is widely deployed and well-understood.

### 12.3 What Changes

**Tier 2 (Working Memory) → Redis Hash:**
```
v1:  self._short[task_id][key] = MemoryEntry
v2:  redis.hset(f"st:{task_id}", key, entry.to_json())
     redis.expire(f"st:{task_id}", task_timeout_seconds)
```

**Tier 3 (Long-Term) → Redis Sorted Set:**
```
v1:  self._long = [MemoryEntry, ...]  (LRU via list)
v2:  redis.zadd("lt:memory", {entry.to_json(): timestamp_epoch})
     # Eviction: redis.zremrangebyrank("lt:memory", 0, -max_capacity-1)
```

**Tier 4 (Episodic) → Redis List per session:**
```
redis.rpush(f"ep:{session_id}", episode.to_json())
redis.expire(f"ep:{session_id}", session_inactivity_timeout)
```

### 12.4 What Stays the Same

The `AgentMemory` class interface does not change. All callers use the same `write_short`, `read_long`, `search_long` methods. The Redis implementation is injected as a backend:

```python
from app.core.memory import AgentMemory, RedisMemoryBackend

memory = AgentMemory(backend=RedisMemoryBackend(redis_url="redis://localhost:6379"))
```

The default backend remains the in-process implementation. The switch to Redis is a configuration change, not a code change for callers.

### 12.5 Consistency Implications

Redis introduces eventual consistency for Tier 3 in a multi-replica configuration. This is acceptable per the consistency model in Section 7. For Tier 2, Redis provides strong consistency within a single instance (single-threaded command processing), which is all that is required.

---

## 13. Observability

The Memory System exposes the following metrics for monitoring and debugging.

### 13.1 Metrics Schema

```python
@dataclass
class MemoryMetrics:
    # Tier 2 metrics
    short_term_active_tasks: int = 0
    short_term_total_entries: int = 0
    short_term_writes: int = 0
    short_term_reads: int = 0
    short_term_hit_rate: float = 0.0    # reads that found a value / total reads
    short_term_clears: int = 0
    short_term_entries_cleared: int = 0

    # Tier 3 metrics
    long_term_total_entries: int = 0
    long_term_capacity: int = 0
    long_term_utilization: float = 0.0  # total_entries / capacity
    long_term_writes: int = 0
    long_term_reads: int = 0
    long_term_hit_rate: float = 0.0
    long_term_evictions: int = 0
    long_term_searches: int = 0
    long_term_avg_search_results: float = 0.0
    long_term_avg_search_latency_ms: float = 0.0

    # Tier 4 metrics (future)
    episodic_active_sessions: int = 0
    episodic_total_episodes: int = 0
    episodic_session_expirations: int = 0
```

### 13.2 Events for Observability

| Event Topic                     | When Emitted                              | Payload Keys                           |
|---------------------------------|-------------------------------------------|----------------------------------------|
| `memory.short_term.written`     | Every `write_short` call                  | `task_id, key, agent_id`               |
| `memory.short_term.cleared`     | Every `clear_task` call                   | `task_id, entries_removed`             |
| `memory.long_term.written`      | Every `write_long` call                   | `key, agent_id, total_entries`         |
| `memory.long_term.evicted`      | When LRU eviction occurs                  | `evicted_key, total_entries_after`     |
| `memory.long_term.searched`     | Every `search_long` call                  | `query_preview, results_returned, latency_ms` |

### 13.3 Exposed via `/health` Endpoint

The `AgentMemory.summarize()` method (existing in v1) is exposed via the AEOS health endpoint:

```json
{
  "memory": {
    "long_term_count": 247,
    "long_term_capacity": 1000,
    "short_term_active_tasks": 3,
    "recent_long_term_keys": ["task_summary.abc", "knowledge.python_async"],
    "agents_seen": ["research_agent", "analyst_agent"]
  }
}
```

---

## 14. Memory Anti-Patterns

These are known misuse patterns that degrade system performance or correctness. They are documented here to be caught in code review.

### Anti-Pattern 1: Writing Large Objects to Long-Term Memory

**What it looks like:**
```python
mem.write_long("full_research_output", huge_text_blob_50kb)
```

**Why it's harmful:** Each Long-Term Memory entry should be a distilled fact or summary, not a full document. Large entries consume disproportionate memory, slow down searches (more text to tokenize), and get evicted sooner (they count as one entry regardless of size).

**Correct approach:** Summarize before writing. Extract key facts and write them as multiple small, well-keyed entries. The full output belongs in Tier 2 (task-scoped) and should not be promoted to Tier 3.

### Anti-Pattern 2: Writing to Long-Term Memory and Never Reading Back

**What it looks like:** An agent writes `knowledge.X` entries but no subsequent agent ever calls `search_long` or `read_long`.

**Why it's harmful:** Pollutes Long-Term Memory, accelerates LRU eviction of genuinely useful entries, and wastes the write operation.

**Correct approach:** Only write to Long-Term Memory if you have a concrete reason to believe the information will be useful to a future task. The question to ask: "Would I look this up again if asked a similar question in the future?"

### Anti-Pattern 3: Skipping `clear_task` on Task End

**What it looks like:** A task completes (or fails) without its Tier 2 Working Memory being cleared.

**Why it's harmful:** Memory leaks. Active tasks count grows without bound. At high concurrency, this can exhaust available memory.

**Correct approach:** Always use the pipeline runner's `finally` block pattern (Section 10.2). Never clear memory manually from agent code.

### Anti-Pattern 4: Using Tier 2 as a Communication Channel Between Concurrent Tasks

**What it looks like:** Task A writes `self._short["task_A"]["shared_result"]` and Task B reads `self._short["task_A"]["shared_result"]` using a hardcoded task_id.

**Why it's harmful:** This breaks the isolation guarantee. Task B now has an implicit dependency on Task A's execution state. If Task A hasn't written yet, Task B gets `None`. If Task A fails, Task B gets an inconsistent view.

**Correct approach:** Cross-task information sharing belongs in Tier 3 (Long-Term Memory) with appropriate key naming. Write from Task A: `mem.write_long("shared.finding_X", result)`. Read from Task B: `mem.read_long("shared.finding_X")`.

### Anti-Pattern 5: Searching Long-Term Memory with High-Frequency Queries

**What it looks like:** An agent calls `search_long(query)` on every cognitive step, including the Act step.

**Why it's harmful:** `search_long` is `O(n × m)`. At 1000 entries and 5 calls per agent step, it becomes a significant fraction of agent latency.

**Correct approach:** Search Long-Term Memory once, in the Observe step, and cache the results in Working Memory: `mem.write_short(task_id, "agent.context.relevant_memory", search_results)`. Subsequent steps read from Tier 2 (O(1)) rather than searching Tier 3 again.

### Anti-Pattern 6: Storing Non-JSON-Safe Values

**What it looks like:**
```python
mem.write_long("agent_instance", actual_agent_object_reference)
mem.write_short(task_id, "coroutine", some_async_coroutine)
```

**Why it's harmful:** Values that are not JSON-safe cannot be serialized for logging, auditing, or future persistence. The `_serialize_value()` method will stringify them, producing unhelpful strings like `"<research_agent object at 0x7f...>"`.

**Correct approach:** Always write data, not objects. Extract the relevant fields and write a dict. Objects belong in runtime state, not in memory.

---

## 15. Cross-References

| Document                        | Relationship                                                                           |
|---------------------------------|----------------------------------------------------------------------------------------|
| `007-AGENT_RUNTIME.md`          | Specifies the 5 cognitive steps (Observe/Orient/Decide/Act/Reflect) that this document maps to memory operations in Section 11 |
| `006-EXECUTION_ENGINE.md`       | The Execution Engine's stages (1, 10, 11, 13, 14, 15) are the primary writers and readers of each memory tier |
| `005-KERNEL.md`                 | The Kernel exposes `AgentMemory` as a singleton service. All agents retrieve the memory instance via the Kernel's service locator |
| `004-EVENT_MODEL.md`            | Memory events (`memory.*` topic domain) are specified in 004. This document specifies when those events are emitted |

---

*End of 009-MEMORY_SYSTEM.md*
