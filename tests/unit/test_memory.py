"""
AEOS Unit Tests — Shared Agent Memory
Tests short-term, long-term, search, eviction, and singleton identity.
"""

import pytest
from app.core.memory import AgentMemory, MemoryEntry


def _mem() -> AgentMemory:
    """Fresh instance per test (not the singleton)."""
    m = AgentMemory.__new__(AgentMemory)
    m._short = {}
    m._long = []
    m._capacity = 5  # small for eviction tests
    return m


# ── Short-term ─────────────────────────────────────────────────────────────────

def test_write_and_read_short_term():
    mem = _mem()
    mem.write_short("task1", "key_a", "value_a", agent_id="ag1")
    assert mem.read_short("task1", "key_a") == "value_a"


def test_read_short_missing_returns_none():
    mem = _mem()
    assert mem.read_short("no_task", "no_key") is None


def test_write_short_overwrites_same_key():
    mem = _mem()
    mem.write_short("t1", "k", "old")
    mem.write_short("t1", "k", "new")
    assert mem.read_short("t1", "k") == "new"


def test_clear_task_removes_all_entries():
    mem = _mem()
    mem.write_short("t1", "a", 1)
    mem.write_short("t1", "b", 2)
    mem.write_short("t2", "c", 3)
    mem.clear_task("t1")
    assert mem.read_short("t1", "a") is None
    assert mem.read_short("t1", "b") is None
    assert mem.read_short("t2", "c") == 3   # other task untouched


def test_clear_nonexistent_task_no_crash():
    mem = _mem()
    mem.clear_task("ghost_task")  # should not raise


def test_get_task_context_returns_snapshot():
    mem = _mem()
    mem.write_short("tx", "x", 10)
    mem.write_short("tx", "y", 20)
    ctx = mem.get_task_context("tx")
    assert ctx == {"x": 10, "y": 20}


def test_get_task_context_empty_task_returns_empty():
    mem = _mem()
    assert mem.get_task_context("absent") == {}


# ── Long-term ──────────────────────────────────────────────────────────────────

def test_write_and_read_long_term():
    mem = _mem()
    mem.write_long("fact:1", {"info": "AEOS is fast"}, agent_id="ag1")
    val = mem.read_long("fact:1")
    assert val == {"info": "AEOS is fast"}


def test_read_long_missing_returns_none():
    mem = _mem()
    assert mem.read_long("nonexistent_key") is None


def test_write_long_same_key_updates():
    mem = _mem()
    mem.write_long("k", "v1")
    mem.write_long("k", "v2")
    assert mem.read_long("k") == "v2"
    # Only one entry with this key
    assert sum(1 for e in mem._long if e.key == "k") == 1


def test_long_term_capacity_eviction():
    mem = _mem()  # capacity=5
    for i in range(7):
        mem.write_long(f"key{i}", f"val{i}")
    assert len(mem._long) == 5
    # Oldest entries evicted; newest survive
    assert mem.read_long("key6") == "val6"
    assert mem.read_long("key5") == "val5"
    assert mem.read_long("key0") is None  # evicted


def test_search_long_returns_relevant_results():
    mem = _mem()
    mem._capacity = 100
    mem.write_long("ml:pipeline", "machine learning data pipeline optimization")
    mem.write_long("db:schema", "database schema migrations postgres")
    mem.write_long("ui:design", "user interface design patterns css")
    results = mem.search_long("machine learning pipeline")
    assert len(results) >= 1
    assert results[0].key == "ml:pipeline"


def test_search_long_no_match_returns_empty():
    mem = _mem()
    mem.write_long("fact", "AEOS orchestration system")
    results = mem.search_long("blockchain quantum computing")
    assert results == []


def test_search_long_empty_store():
    mem = _mem()
    assert mem.search_long("anything") == []


# ── Summarize ──────────────────────────────────────────────────────────────────

def test_summarize_reflects_state():
    mem = _mem()
    mem._capacity = 100
    mem.write_short("t1", "k", "v")
    mem.write_long("longkey", "longval", agent_id="my_agent")
    s = mem.summarize()
    assert s["long_term_count"] == 1
    assert s["short_term_active_tasks"] == 1
    assert "my_agent" in s["agents_seen"]
    assert "longkey" in s["recent_long_term_keys"]


# ── Memory entry dataclass ─────────────────────────────────────────────────────

def test_memory_entry_fields():
    mem = _mem()
    mem.write_long("k", "v", agent_id="a1", task_id="t1")
    entry = mem._long[-1]
    assert isinstance(entry, MemoryEntry)
    assert entry.key == "k"
    assert entry.agent_id == "a1"
    assert entry.task_id == "t1"
    assert entry.memory_type == "long"
    assert entry.timestamp != ""


# ── Singleton ──────────────────────────────────────────────────────────────────

def test_get_memory_singleton():
    from app.core.memory import get_memory
    a = get_memory()
    b = get_memory()
    assert a is b
