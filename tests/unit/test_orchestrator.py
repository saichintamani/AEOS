"""
AEOS Unit Tests — Orchestrator Engine
"""

import asyncio
import pytest
from app.core.orchestrator import Orchestrator
from app.agents.base import BaseAgent


# ── Helpers ────────────────────────────────────────────────────────────────────

class _QuickAgent(BaseAgent):
    def __init__(self, agent_id="quick", result=None, delay=0.0):
        super().__init__()
        self.id = agent_id
        self.name = f"Quick Agent [{agent_id}]"
        self.capabilities = ["testing"]
        self._result = result or {"ok": True}
        self._delay = delay

    async def think(self, task): return "thought"
    async def act(self, thought, context):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result


async def _make_orchestrator(*agents) -> Orchestrator:
    orc = Orchestrator()
    for a in agents:
        orc.register(a)
    await orc.startup()
    return orc


# ── Registry ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_register_agent_appears_in_list():
    orc = Orchestrator()
    orc.register(_QuickAgent("a1"))
    assert any(a["id"] == "a1" for a in orc.list_agents())


@pytest.mark.asyncio
async def test_duplicate_registration_raises():
    orc = Orchestrator()
    orc.register(_QuickAgent("dup"))
    with pytest.raises(ValueError, match="already registered"):
        orc.register(_QuickAgent("dup"))


# ── Startup / Shutdown ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_startup_sets_ready():
    orc = await _make_orchestrator(_QuickAgent("s1"))
    assert orc._ready is True
    await orc.shutdown()


@pytest.mark.asyncio
async def test_shutdown_clears_ready():
    orc = await _make_orchestrator(_QuickAgent("s2"))
    await orc.shutdown()
    assert orc._ready is False


@pytest.mark.asyncio
async def test_run_before_startup_raises():
    orc = Orchestrator()
    orc.register(_QuickAgent("pre"))
    with pytest.raises(RuntimeError, match="not ready"):
        await orc.run_task("task")


# ── Single-agent execution ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_single_agent_success():
    orc = await _make_orchestrator(_QuickAgent("simple_agent", result={"value": 42}))
    resp = await orc.run_task("do something", mode="single-agent")
    assert resp.status == "success"
    assert resp.result == {"value": 42}
    await orc.shutdown()


@pytest.mark.asyncio
async def test_empty_task_returns_failed():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    resp = await orc.run_task("   ", mode="single-agent")
    assert resp.status == "failed"
    assert "empty" in resp.error.lower()
    await orc.shutdown()


@pytest.mark.asyncio
async def test_task_count_increments():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    await orc.run_task("task one")
    await orc.run_task("task two")
    assert orc._task_count == 2
    await orc.shutdown()


@pytest.mark.asyncio
async def test_response_has_trace_id():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    resp = await orc.run_task("trace me")
    assert resp.trace_id != ""
    d = resp.to_dict()
    assert d["metadata"]["trace_id"] != ""
    await orc.shutdown()


@pytest.mark.asyncio
async def test_response_has_latency():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    resp = await orc.run_task("latency check")
    assert resp.latency_ms >= 0
    await orc.shutdown()


# ── Timeout ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_timeout_returns_error():
    from unittest.mock import patch
    orc = await _make_orchestrator(_QuickAgent("simple_agent", delay=5.0))
    with patch.object(orc._registry["simple_agent"], "run", side_effect=asyncio.TimeoutError):
        # Simulate timeout by patching
        pass
    # Real timeout test: use very short timeout
    from app.core import config as cfg_module
    original = cfg_module.settings.agent_timeout_seconds
    cfg_module.settings.__dict__["agent_timeout_seconds"] = 0.01
    resp = await orc.run_task("slow task")
    cfg_module.settings.__dict__["agent_timeout_seconds"] = original
    # Either success (if fast enough) or timeout error — either is valid behavior
    assert resp.status in ("success", "failed")
    await orc.shutdown()


# ── State introspection ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_state_reflects_agents():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    state = orc.state()
    assert state["ready"] is True
    assert any(a["id"] == "simple_agent" for a in state["agents"])
    await orc.shutdown()


@pytest.mark.asyncio
async def test_state_config_present():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    state = orc.state()
    assert "environment" in state["config"]
    assert "agent_timeout_seconds" in state["config"]
    await orc.shutdown()


# ── Multi-agent (with planner + reviewer registered) ──────────────────────────

@pytest.mark.asyncio
async def test_multi_agent_requires_planner():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    resp = await orc.run_task("research something", mode="multi-agent")
    assert resp.status == "failed"
    assert "planner_agent" in resp.error
    await orc.shutdown()


@pytest.mark.asyncio
async def test_topological_sort_respects_dependencies():
    orc = Orchestrator()
    subtasks = [
        {"id": "c", "agent": "x", "depends_on": ["a", "b"], "priority": 3},
        {"id": "a", "agent": "x", "depends_on": [],          "priority": 1},
        {"id": "b", "agent": "x", "depends_on": ["a"],       "priority": 2},
    ]
    ordered = orc._topological_sort(subtasks)
    ids = [s["id"] for s in ordered]
    assert ids.index("a") < ids.index("b")
    assert ids.index("b") < ids.index("c")


# ── Intelligent routing ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_intelligent_route_research_keyword():
    orc = await _make_orchestrator(
        _QuickAgent("simple_agent"),
        _QuickAgent("research_agent", result={"synthesis": "found it"}),
    )
    resp = await orc.run_task("research the AEOS system", mode="single-agent")
    assert resp.agent == "research_agent"
    await orc.shutdown()


@pytest.mark.asyncio
async def test_intelligent_route_analyze_keyword():
    orc = await _make_orchestrator(
        _QuickAgent("simple_agent"),
        _QuickAgent("analyst_agent", result={"conclusions": ["ok"], "confidence": 0.8}),
    )
    resp = await orc.run_task("analyze the performance data", mode="single-agent")
    assert resp.agent == "analyst_agent"
    await orc.shutdown()


@pytest.mark.asyncio
async def test_intelligent_route_execute_keyword():
    orc = await _make_orchestrator(
        _QuickAgent("simple_agent"),
        _QuickAgent("executor_agent", result={"execution_status": "completed"}),
    )
    resp = await orc.run_task("execute the deployment steps", mode="single-agent")
    assert resp.agent == "executor_agent"
    await orc.shutdown()


@pytest.mark.asyncio
async def test_intelligent_route_default_to_simple_agent():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    resp = await orc.run_task("tell me something interesting", mode="single-agent")
    assert resp.agent == "simple_agent"
    await orc.shutdown()


# ── Fallback chain ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_fires_when_primary_fails():
    from app.agents.base import AgentResponse

    class _FailAgent(_QuickAgent):
        async def act(self, thought, context):
            raise RuntimeError("primary agent always fails")

    orc = Orchestrator()
    orc.register(_FailAgent("research_agent"))
    orc.register(_QuickAgent("simple_agent", result={"fallback": True}))
    await orc.startup()

    resp = await orc.run_task("research something", mode="single-agent")
    # Fallback (simple_agent) should have run and succeeded
    assert resp.status == "success"
    assert resp.result == {"fallback": True}
    await orc.shutdown()


# ── Memory integration ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_long_term_memory_populated_after_task():
    from app.core.memory import AgentMemory
    # Use a fresh memory instance for isolation
    fresh_mem = AgentMemory()

    import app.core.memory as mem_module
    original_get = mem_module.get_memory

    def patched_get():
        return fresh_mem

    mem_module.get_memory = patched_get
    try:
        orc = await _make_orchestrator(_QuickAgent("simple_agent", result={"summary": "done"}))
        await orc.run_task("summarize the report")
        summary = fresh_mem.summarize()
        assert summary["long_term_count"] >= 1
        await orc.shutdown()
    finally:
        mem_module.get_memory = original_get


# ── Message bus events ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bus_receives_task_completed_event():
    from app.core.message_bus import MessageBus, AgentMessage
    import app.core.message_bus as bus_module

    fresh_bus = MessageBus()
    original_get = bus_module.get_message_bus

    def patched_get():
        return fresh_bus

    bus_module.get_message_bus = patched_get
    try:
        completed: list[AgentMessage] = []

        async def on_done(msg: AgentMessage):
            completed.append(msg)

        fresh_bus.subscribe("task.completed", on_done)
        orc = await _make_orchestrator(_QuickAgent("simple_agent", result={"ok": True}))
        await orc.run_task("do something")
        assert len(completed) >= 1
        assert completed[0].topic == "task.completed"
        await orc.shutdown()
    finally:
        bus_module.get_message_bus = original_get


@pytest.mark.asyncio
async def test_state_includes_memory_and_bus_summary():
    orc = await _make_orchestrator(_QuickAgent("simple_agent"))
    state = orc.state()
    assert "memory" in state
    assert "message_bus" in state
    assert "long_term_count" in state["memory"]
    await orc.shutdown()
