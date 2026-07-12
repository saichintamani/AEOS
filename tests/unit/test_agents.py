"""
AEOS Unit Tests — Agent Layer
Tests all four agents: lifecycle, output schema, edge cases.
"""

import pytest
from app.agents.base import BaseAgent, AgentResponse


# ── Helpers ────────────────────────────────────────────────────────────────────

class _FakeAgent(BaseAgent):
    def __init__(self, result=None, fail=False):
        super().__init__()
        self.id = "fake_agent"
        self.name = "Fake Test Agent"
        self.capabilities = ["testing"]
        self._result = result or {"ok": True}
        self._fail = fail

    async def think(self, task):
        return "fake thought"

    async def act(self, thought, context):
        if self._fail:
            raise RuntimeError("Intentional test failure")
        return self._result


# ── BaseAgent contract ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_base_agent_not_initialized_raises():
    agent = _FakeAgent()
    with pytest.raises(RuntimeError, match="not initialized"):
        await agent.run("hello", {})


@pytest.mark.asyncio
async def test_base_agent_lifecycle():
    agent = _FakeAgent()
    await agent.initialize()
    response = await agent.run("test task", {"original_task": "test task"})
    assert isinstance(response, AgentResponse)
    assert response.status == "success"
    assert response.agent_id == "fake_agent"


@pytest.mark.asyncio
async def test_base_agent_failure_returns_failed_status():
    agent = _FakeAgent(fail=True)
    await agent.initialize()
    response = await agent.run("fail", {"original_task": "fail"})
    assert response.status == "failed"
    assert response.error != ""
    assert response.result is None


@pytest.mark.asyncio
async def test_base_agent_describe():
    agent = _FakeAgent()
    d = agent.describe()
    assert d["id"] == "fake_agent"
    assert "capabilities" in d
    assert isinstance(d["capabilities"], list)


# ── SimpleAgent ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_simple_agent_full_lifecycle():
    from app.agents.simple_agent import SimpleAgent
    agent = SimpleAgent()
    await agent.initialize()
    response = await agent.run("train a classification model", {"original_task": "train a classification model"})
    assert response.status == "success"
    assert "domain" in response.result
    assert response.result["domain"] == "machine_learning"


@pytest.mark.asyncio
async def test_simple_agent_keyword_extraction():
    from app.agents.simple_agent import SimpleAgent
    agent = SimpleAgent()
    await agent.initialize()
    response = await agent.run("analyze codebase", {"original_task": "analyze codebase"})
    assert "keywords" in response.result
    assert isinstance(response.result["keywords"], list)
    assert len(response.result["keywords"]) > 0


@pytest.mark.asyncio
async def test_simple_agent_complexity_score_range():
    from app.agents.simple_agent import SimpleAgent
    agent = SimpleAgent()
    await agent.initialize()
    response = await agent.run("x", {"original_task": "x"})
    score = response.result["complexity_score"]
    assert 1 <= score <= 10


@pytest.mark.asyncio
async def test_simple_agent_subtasks_nonempty():
    from app.agents.simple_agent import SimpleAgent
    agent = SimpleAgent()
    await agent.initialize()
    response = await agent.run("find bugs and then fix them", {"original_task": "find bugs and then fix them"})
    assert "subtasks" in response.result
    assert len(response.result["subtasks"]) > 0


# ── PlannerAgent ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_planner_agent_produces_dag():
    from app.agents.planner_agent import PlannerAgent
    agent = PlannerAgent()
    await agent.initialize()
    response = await agent.run("research ML pipeline", {"original_task": "research ML pipeline"})
    assert response.status == "success"
    plan = response.result
    assert "subtasks" in plan
    assert isinstance(plan["subtasks"], list)
    assert len(plan["subtasks"]) > 0


@pytest.mark.asyncio
async def test_planner_dag_has_agent_assignments():
    from app.agents.planner_agent import PlannerAgent
    agent = PlannerAgent()
    await agent.initialize()
    response = await agent.run("find code bugs", {"original_task": "find code bugs"})
    for subtask in response.result["subtasks"]:
        assert "agent" in subtask
        assert "id" in subtask
        assert "depends_on" in subtask


@pytest.mark.asyncio
async def test_planner_classifies_ml_task():
    from app.agents.planner_agent import PlannerAgent
    agent = PlannerAgent()
    await agent.initialize()
    response = await agent.run("train a neural network model", {"original_task": "train a neural network model"})
    assert response.result["task_type"] == "ml"


# ── ResearchAgent ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_research_agent_empty_store_returns_result():
    from app.agents.research_agent import ResearchAgent
    agent = ResearchAgent()
    await agent.initialize()
    response = await agent.run("what is AEOS?", {"original_task": "what is AEOS?"})
    assert response.status == "success"
    assert "synthesis" in response.result
    assert "findings" in response.result


@pytest.mark.asyncio
async def test_research_agent_with_ingested_data():
    from app.agents.research_agent import ResearchAgent
    from app.rag.rag_engine import get_rag_engine
    # Use isolated namespace (RAGEngine wraps a pipeline; build via the factory)
    engine = get_rag_engine("test_research")
    engine.reset()  # clean any persisted data from a prior run
    await engine.initialize()
    engine.ingest_text(
        "AEOS is a production-grade AI Engineering Orchestration System built with FastAPI.",
        source="test_doc",
    )
    agent = ResearchAgent()
    agent._rag_engine = engine
    agent._initialized = True
    response = await agent.run("what is AEOS?", {"original_task": "what is AEOS?"})
    assert response.status == "success"
    assert response.result["sources_found"] >= 1
    engine.reset()


# ── ReviewerAgent ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reviewer_pass_verdict_on_good_result():
    from app.agents.reviewer_agent import ReviewerAgent
    agent = ReviewerAgent()
    await agent.initialize()
    ctx = {
        "original_task": "analyze X",
        "previous_result": {
            "summary": "Complete analysis",
            "findings": ["finding1", "finding2"],
            "confidence": 0.9,
            "recommendation": "Use approach A",
        },
        "revision_round": 0,
    }
    response = await agent.run("analyze X", ctx)
    assert response.status == "success"
    assert response.result["verdict"] == "PASS"


@pytest.mark.asyncio
async def test_reviewer_revise_verdict_on_weak_result():
    from app.agents.reviewer_agent import ReviewerAgent
    agent = ReviewerAgent()
    await agent.initialize()
    ctx = {
        "original_task": "analyze X",
        "previous_result": {"x": None},
        "revision_round": 0,
    }
    response = await agent.run("analyze X", ctx)
    assert response.result["verdict"] in ("REVISE", "REJECT")


@pytest.mark.asyncio
async def test_reviewer_reject_on_none_result():
    from app.agents.reviewer_agent import ReviewerAgent
    agent = ReviewerAgent()
    await agent.initialize()
    ctx = {"original_task": "task", "previous_result": None, "revision_round": 0}
    response = await agent.run("task", ctx)
    assert response.result["verdict"] == "REJECT"


@pytest.mark.asyncio
async def test_reviewer_has_feedback_and_scores():
    from app.agents.reviewer_agent import ReviewerAgent
    agent = ReviewerAgent()
    await agent.initialize()
    ctx = {"original_task": "task", "previous_result": {"a": 1, "b": 2}, "revision_round": 0}
    response = await agent.run("task", ctx)
    assert "scores" in response.result
    assert "feedback" in response.result
    assert "overall" in response.result["scores"]


# ── AnalystAgent ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_analyst_agent_full_lifecycle():
    from app.agents.analyst_agent import AnalystAgent
    agent = AnalystAgent()
    await agent.initialize()
    ctx = {
        "original_task": "analyze the system performance",
        "previous_result": {"throughput": "1000 req/s", "latency": "5ms", "errors": "0.1%"},
    }
    response = await agent.run("analyze the system performance", ctx)
    assert response.status == "success"
    assert "facts" in response.result
    assert "conclusions" in response.result
    assert "analysis_type" in response.result


@pytest.mark.asyncio
async def test_analyst_agent_returns_facts_and_conclusions():
    from app.agents.analyst_agent import AnalystAgent
    agent = AnalystAgent()
    await agent.initialize()
    ctx = {
        "original_task": "evaluate performance metrics",
        "previous_result": {"speed": "fast", "accuracy": "99%", "reliability": "high"},
    }
    response = await agent.run("evaluate performance metrics", ctx)
    assert isinstance(response.result["facts"], list)
    assert isinstance(response.result["conclusions"], list)
    assert len(response.result["facts"]) >= 1
    assert len(response.result["conclusions"]) >= 1


@pytest.mark.asyncio
async def test_analyst_agent_confidence_in_range():
    from app.agents.analyst_agent import AnalystAgent
    agent = AnalystAgent()
    await agent.initialize()
    ctx = {"original_task": "assess quality", "previous_result": {"q": "good"}}
    response = await agent.run("assess quality", ctx)
    conf = response.result["confidence"]
    assert 0.0 <= conf <= 1.0


@pytest.mark.asyncio
async def test_analyst_agent_handles_empty_previous_result():
    from app.agents.analyst_agent import AnalystAgent
    agent = AnalystAgent()
    await agent.initialize()
    ctx = {"original_task": "analyze nothing", "previous_result": None}
    response = await agent.run("analyze nothing", ctx)
    assert response.status == "success"
    assert response.result["evidence_strength"] == "weak"


@pytest.mark.asyncio
async def test_analyst_agent_evidence_strength_field():
    from app.agents.analyst_agent import AnalystAgent
    agent = AnalystAgent()
    await agent.initialize()
    ctx = {"original_task": "compare options", "previous_result": {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5, "f": 6, "g": 7, "h": 8}}
    response = await agent.run("compare options", ctx)
    assert response.result["evidence_strength"] in ("strong", "moderate", "weak")


# ── ExecutorAgent ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_executor_agent_full_lifecycle():
    from app.agents.executor_agent import ExecutorAgent
    agent = ExecutorAgent()
    await agent.initialize()
    ctx = {
        "original_task": "summarize the findings",
        "previous_result": {"summary": "Key findings here", "details": "More info"},
    }
    response = await agent.run("summarize the findings", ctx)
    assert response.status == "success"
    assert "execution_log" in response.result
    assert "actions_executed" in response.result


@pytest.mark.asyncio
async def test_executor_agent_returns_execution_log():
    from app.agents.executor_agent import ExecutorAgent
    agent = ExecutorAgent()
    await agent.initialize()
    ctx = {"original_task": "format the output", "previous_result": {"key": "value"}}
    response = await agent.run("format the output", ctx)
    log = response.result["execution_log"]
    assert isinstance(log, list)
    assert len(log) >= 1


@pytest.mark.asyncio
async def test_executor_agent_unsupported_action_no_crash():
    from app.agents.executor_agent import ExecutorAgent
    agent = ExecutorAgent()
    await agent.initialize()
    # "delete" is not a supported action
    ctx = {"original_task": "delete all records", "previous_result": {}}
    response = await agent.run("delete all records", ctx)
    assert response.status == "success"  # agent succeeds even if all actions unsupported
    assert any(r["status"] == "unsupported" for r in response.result["actions_executed"])


@pytest.mark.asyncio
async def test_executor_agent_partial_completion():
    from app.agents.executor_agent import ExecutorAgent
    agent = ExecutorAgent()
    await agent.initialize()
    # "validate and format" → two supported actions
    ctx = {
        "original_task": "validate and format the data",
        "previous_result": {"field1": "val1", "field2": ""},
    }
    response = await agent.run("validate and format the data", ctx)
    assert response.result["success_count"] >= 1


@pytest.mark.asyncio
async def test_executor_agent_execution_status_field():
    from app.agents.executor_agent import ExecutorAgent
    agent = ExecutorAgent()
    await agent.initialize()
    ctx = {"original_task": "summarize results", "previous_result": {"x": "y"}}
    response = await agent.run("summarize results", ctx)
    assert response.result["execution_status"] in ("completed", "partial", "failed")
